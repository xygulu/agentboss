# libs

第三方 jar，构建时由 `build.sh` 的 `javac -classpath` 与 `d8` 位置参数一起使用。

## core-3.5.3.jar

- 来源：ZXing（Zebra Crossing）core，`https://maven.aliyun.com/repository/public/com/google/zxing/core/3.5.3/core-3.5.3.jar`
  （阿里云 Maven 镜像；Maven Central 在本机网络不可达）
- 许可：Apache License 2.0（https://github.com/zxing/zxing）
- SHA-256：`8d8064c1636fdaef7189dd9055c7d59950a8940a12f2293956446ec3c109fd82`
- 用途：`ScanActivity` 解码二维码（`MultiFormatReader` + `PlanarYUVLuminanceSource`），
  页面运行在非安全上下文（http），`getUserMedia` 不可用，只能原生解码。

重新获取：

```bash
curl -fL -o libs/core-3.5.3.jar \
  "https://maven.aliyun.com/repository/public/com/google/zxing/core/3.5.3/core-3.5.3.jar"
```
