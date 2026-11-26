# SPDX-License-Identifier: AGPL-3.0-or-later

"""桌面工头 Host 侧：桌宠导演 + 板子接口。

Host 只多三个钩子：add_routes(app)、start(app)、stop(app)。
"""

from pathlib import Path


def add_routes(app):
    from .api import add_routes as _add
    _add(app)


async def start(app):
    import notify
    from .director import PetDirector
    from .gate import phone_voice
    from .presence import MacPresenceSampler
    from .store import Store

    d = PetDirector(Store(Path(__file__).resolve().parent.parent / "pet.json"),
                    MacPresenceSampler())
    app["pet"] = d
    notify.set_pet_gate(phone_voice)
    await d.start()


async def stop(app):
    import notify
    notify.set_pet_gate(None)
    d = app.get("pet")
    if d:
        await d.stop()
