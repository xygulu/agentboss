# OSS 导出替换表（sed -E；只动完整域名/URL/路径/包名，裸词 codeboss 绝不替换）
s/relay\.codeboss\.work/relay.example.com/g
s/dl\.example\.top/download.example.com/g
s/codeboss\.example\.top/relay.example.com/g
s/top\.example\./com.example./g
s/(www\.)?codeboss\.me/example.com/g
s/github\.com\/example\//github.com\/example\//g
s/testflight\.apple\.com\/join\/[A-Za-z0-9]+/testflight.apple.com\/join\/XXXXXXXX/g
s/浙ICP备[0-9]+号//g
s/\/Users\/xuxingyu/\/home\/dev/g
s|~/.codeboss|~/.codeboss|g
# 兜底：example 是官方身份词（仓名/残余子域），导出物里必须归零
s/example/example/g
