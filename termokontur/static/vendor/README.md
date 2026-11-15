# Browser flasher dependencies

The browser serves this bundle from the Termokontur origin; it makes no CDN
requests. The installer is based on Espressif's Apache-2.0 `esptool-js` 0.5.7.
The exact runtime and build dependencies are pinned in
`scripts/vendor-flasher/package-lock.json`. MD5 verification uses `spark-md5`
3.0.2. Complete dependency licenses are in `FLASHER-LICENSES.txt`.

Rebuild from the repository root:

```sh
npm ci --prefix scripts/vendor-flasher
npm run --prefix scripts/vendor-flasher build
```

The pinned release accepts binary strings in FlashOptions.fileArray. Its own
published TypeScript definitions are used for the wrapper API. ESP32-C3 is
checked before writing; SHA-256 checks the downloaded common image, and the
loader compares the programmed image's MD5 with the device's flash result.

Official references:

- https://github.com/espressif/esptool-js
- https://www.npmjs.com/package/esptool-js/v/0.5.7
- https://docs.espressif.com/projects/esp-idf/en/v5.3.1/esp32c3/api-guides/usb-serial-jtag-console.html

Physical USB transfer, board resets and the power transition still require
verification on an actual ESP32-C3 SuperMini. Browser tests simulate USB;
they do not certify a physical flashing cycle.
