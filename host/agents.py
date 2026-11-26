#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""agent 适配层。

host 里有一堆「跟具体 agent 绑死」的活：找活着的会话、读转录、装 hook、开进程、
认弹层、报模型名。现在这些活全都写死 Claude Code（server.py 里约 850 行）。

把它抽成接口之后：加一个 Codex 只需要写一个适配器，
「跟 agent 无关的骨架」一行都不用动 —— 全彩镜像、按键字节表、中继隧道与计时、
通知引擎、语音、手机壳，这些本来就是 agent 无关的。

约定：
- 适配器只描述「这个 agent 怎么找、怎么读、怎么开、怎么认弹层」，不碰别的副作用。
- Claude 的实现在 server.py 里（老逻辑一行没改，只是被包成适配器）；
  Codex 的实现在 agent_codex.py。
- 基类里的默认实现都是「不支持」：返回空 / None。调用方必须兜住，
  缺能力要显示出来，不能静默当成功。
"""


DEFAULT_AGENT = "claude"


class AgentAdapter:
    """一个 agent（Claude Code / Codex / …）的适配器。

    方法全是「能做什么就做什么」：默认实现返回空，表示这个 agent 不支持该能力。
    """

    key = ""            # agent 标识：进会话字典、进登记表
    label = ""          # 显示名（手机端会话卡片上的小标签）
    supports_fork = True   # 能不能「复制一份历史」（Codex 不行 → 明确报错，不假装）
    eager_session = True   # 一启动就有会话 id（Claude）；False = 等第一句话（Codex）
    # 能力位：手机端数据驱动渲染（caps 数组跟着 /api/ext_sessions 走），默认都不支持
    can_resume = False        # 能否从盘上记录召回历史会话
    can_notify = False        # 能否装「跑完提醒」钩子
    can_model_switch = False  # 能否启动时指定模型
    protocol = False          # True = 协议源（ACP 等）：开窗走 stdio 直连，不包 PTY

    # ---- 会话发现 ----
    def discover(self):
        """活着的会话。

        返回的每个 dict 至少要有：sid / short / pid / cwd / title / window_id / tty / state，
        再带一个 agent = 自己的 key。
        """
        return []

    # ---- 转录 ----
    def transcript_path(self, cwd, sid):
        """这个会话的转录文件；没有就 None。"""
        return None

    def parse_transcript(self, path, limit=120):
        """转录 → [{role, text, tools:[{name, detail}]}]。"""
        return []

    def transcript_meta(self, path):
        """转录 → (cwd, 标题)。"""
        return ("", "")

    def closed_sessions(self, live_sids, limit=50):
        """已退出、但记录还在盘上的会话（可召回），按最近活跃排。"""
        return []

    def delete_transcript(self, sid):
        """删掉这个会话在盘上的记录。返回删掉的文件数。"""
        return 0

    # ---- 起进程 ----
    def installed(self):
        """这台机器上到底装没装这个 agent。

        手机端「＋」据此决定：只有一种 agent 就直接开，两种都装就先问一句。
        默认 False：不知道就当没装，别把一个打不开的选项摆给用户。"""
        return False

    def prepare_cwd(self, cwd):
        """在这个目录里开会话前的准备（信任目录、补默认配置…）。

        默认什么都不用做。返回 False 表示没准备好（调用方可以照常开，
        但要在日志里留一句）。"""
        return True

    def launch_argv(self, cwd, sid=None, model=None):
        """agent 自己的 argv（不含 wrapper 前缀）。

        sid 非空 = 召回旧会话；model 非空 = 指定模型。
        返回 None = 这台机器上没装这个 agent，调用方要把话说明白。
        """
        return None

    def fork_argv(self, cwd, sid):
        """复制一份历史：返回 argv；None = 不支持 fork。"""
        return None

    # ---- 提醒 hook ----
    def hook_spec(self):
        """要接的事件：[(event, matcher)]，matcher 为 None 表示不过滤。"""
        return []

    def hook_install(self, on):
        """装/卸 hook。返回 0 成功、1 失败。"""
        return 0

    def hooks_installed(self):
        """hook 是不是都还挂着（用来做自愈：被别的程序改掉了就接回去）。"""
        return True

    def hook_status(self):
        """(已接的事件列表, 配置文件路径)——给 --hooks status 看。"""
        return ([], "")

    # ---- 界面 ----
    def dialog_hints(self):
        """弹层底栏的识别词（这个 agent 的 TUI 文案）：命中 ≥2 个才算底栏。"""
        return ()

    def dialog_prefixes(self):
        """弹层底栏的开头词（整行以其中之一开头就认）。"""
        return ()

    def agent_keys(self):
        """这个 agent 独有的快捷键（手机键栏按会话类型显示）。"""
        return ()

    def model_info(self):
        """当前模型名（显示用）。"""
        return ""


_ADAPTERS = {}      # key -> AgentAdapter
_AGENT_OF = {}      # sid -> agent key（进程内缓存）
_LOOKUP = None      # server.py 注册的「sid -> agent key」外部查法（查登记表）


def register(ad):
    """注册一个适配器。同 key 后注册的覆盖前面的。"""
    _ADAPTERS[ad.key] = ad
    return ad


def adapters():
    return list(_ADAPTERS.values())


def get(key):
    """按 key 取适配器。

    没给 key（老数据）→ 默认 agent；给了 key 但这个进程里没注册对应适配器
    （比如登记表里写着 codex，但这台机器/这个版本没有 codex 适配器）→ None。
    宁可返回 None 让调用方把「没装/不支持」显示出来，也不能悄悄当成 Claude 去操作。"""
    if not key:
        return _ADAPTERS.get(DEFAULT_AGENT)
    return _ADAPTERS.get(key)


def default():
    return _ADAPTERS.get(DEFAULT_AGENT)


def set_lookup(fn):
    """注册外部查法：fn(sid) -> agent key 或 None。

    server.py 用它查登记表（进程重启后内存缓存没了，登记表还在）。
    """
    global _LOOKUP
    _LOOKUP = fn


def note_agent(sid, key):
    """记住「这个 sid 是哪个 agent 的」。"""
    if sid and key:
        _AGENT_OF[sid] = key


def forget_agents():
    """清掉内存缓存（登记表里没有的会话会被重新探测）。"""
    _AGENT_OF.clear()


def agent_of(sid, cwd=""):
    """这个 sid 是哪个 agent 的会话。查不到就当 claude（保持老行为）。"""
    if not sid:
        return DEFAULT_AGENT
    k = _AGENT_OF.get(sid)
    if k:
        return k
    if _LOOKUP:
        try:
            k = _LOOKUP(sid)
        except Exception:
            k = None
        if k:
            note_agent(sid, k)
            return k
    # 兜底：谁的转录在盘上，就是谁的会话
    for ad in adapters():
        try:
            if ad.transcript_path(cwd, sid):
                note_agent(sid, ad.key)
                return ad.key
        except Exception:
            pass
    return DEFAULT_AGENT


def adapter_of(sid, cwd=""):
    """取这个会话对应的适配器对象。"""
    return get(agent_of(sid, cwd))
