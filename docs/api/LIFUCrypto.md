# `LIFUCrypto` API

SBSFU firmware image signing, validation and inspection. This module is the
**single source of truth** for the LIFU secure-bootloader image format and the
`FwVersion` encoding — the SDK's update/migration tooling, the standalone CLI,
and the firmware-build CI all sign and verify through it.

Image format (`SECBOOT_ECCDSA_WITH_AES128_CBC_SHA256`, NO_LOADER config):

```
[320 B header] [0xFF pad to 0x400] [firmware body, stored in clear]
```

The 128-byte authenticated header region carries the metadata and the SHA-256
of the firmware body (`FwTag`), signed with ECDSA-P256/SHA-256. At boot the
bootloader verifies the header signature and the body hash; the AES fields
exist only to satisfy the header format and are unused in this configuration.

Installation
- `pip install "openlifu-sdk[crypto]"` — signing/verification needs the
  optional `cryptography` dependency. Parsing and hash checks work without it.
- The module also runs **standalone** (guarded imports): executing the file
  directly requires only `cryptography`, not the rest of the SDK.

FwVersion encoding (16-bit bitfield)
- `encode_fw_version(version: int | str) -> int` — accepts an already-encoded
  int (1–65535) or a semver string; git-describe/pre-release suffixes are
  dropped (`"1.2.6-rc.1-3-gabc"` encodes as `1.2.6`).
- `decode_fw_version(value: int) -> str` — back to `"major.minor.patch"`.
- Layout: `major[15:11] . minor[10:5] . patch[4:0]` → ranges **major 0–31,
  minor 0–63, patch 0–31** (max `31.63.31` = 0xFFFF; `0.0.0` is invalid).
  Strictly monotonic with semver, so the bootloader's anti-rollback integer
  compare needs no knowledge of the scheme.
- **Migration note:** this replaces the earlier decimal `MMmmpp` convention
  (`1.2.6` was `10206`, is now `2118`). Units with a floor latched under the
  old scheme reject new-scheme images until the floor is reset (full-chip
  erase / migration).
- `read_fw_version_header(header_path) -> dict` — parse a CMake-generated
  `version.h` (`FW_VERSION`, `FW_SHA`, `FW_BUILD_TIME`) so builds can be
  signed from their own version metadata.

Keys
- `SigningKeys.from_directory(keys_dir, require_private=False) -> SigningKeys`
  — loads from a keys directory: `ecdsa_private.pem` + `aes128.bin` (signing),
  `ecdsa_public.pem` (validation; derived from the private key when absent).
  Key material is never bundled with the SDK — every operation takes an
  explicit keys directory.

Signing
- `sign_firmware_file(firmware, keys_dir, output, version) -> FirmwareHeader`
  — sign a raw application `.bin` (linked for slot base + 0x400) and write the
  signed image; returns the parsed header.
- `sign_firmware_bytes(firmware: bytes, keys: SigningKeys, version) -> bytes`
  — the in-memory core.

Parsing and inspection
- `parse_signed_image(path | bytes) -> FirmwareHeader` — parse the 320-byte
  header of a signed image (or a dump of the active slot).
- `FirmwareHeader` — dataclass with `magic`, `protocol_version`, `fw_version`
  (+ `fw_version_str`), `fw_size`, `fw_tag`, `signature`, `image_state`
  (+ `image_state_str`), `prev_fingerprint`; `describe()` returns a
  human-readable summary.

Validation
- `validate_signed_image(image, keys_dir=None) -> ValidationReport` — re-runs
  everything the bootloader checks before launch: magic, protocol, sizes,
  `FwTag` SHA-256, and (when a public key is available) the ECDSA header
  signature. Accepts a full slot dump; bytes past `FwSize` are ignored.
- `ValidationReport` — per-check booleans (`magic_ok`, `fw_tag_ok`,
  `signature_ok`, …), `structural_ok`, `ok`, and a three-state `verdict`:
  `VALID`, `UNVERIFIED` (structure/hashes pass, no key for the signature
  check), or `NOT VALID`. `describe()` prints the check table.

Errors
- `LIFUCryptoError` — signing/validation failures (missing keys, malformed
  image). Subclasses the SDK's `LIFUError` when the full SDK is installed.

Command line

```
python -m openlifu_sdk.io.LIFUCrypto sign   --keys DIR --firmware app.bin \
        --version 1.2.7 --output app_signed.bin
python -m openlifu_sdk.io.LIFUCrypto sign   --keys DIR --firmware app.bin \
        --version-header build/Release/generated/version.h --output app_signed.bin
python -m openlifu_sdk.io.LIFUCrypto verify app_signed.bin --keys DIR   # exit 0/1
python -m openlifu_sdk.io.LIFUCrypto info   app_signed.bin [--keys DIR]
```

`--version` takes a semver string or a raw encoded integer;
`--version-header` reads `FW_VERSION` from the firmware build's generated
`version.h` instead. `verify` is CI-friendly (exit code); `info` prints the
header metadata plus the validation table.

Usage example

```py
from openlifu_sdk.io.LIFUCrypto import sign_firmware_file, validate_signed_image

hdr = sign_firmware_file(
    firmware="lifu-console-fw.bin", keys_dir="bl-keys/console",
    output="lifu-console-fw_signed.bin", version="1.2.7")
print(hdr.fw_version, hdr.fw_version_str)      # 2119 1.2.7

report = validate_signed_image("lifu-console-fw_signed.bin",
                               keys_dir="bl-keys/console")
assert report.ok
```

See also: `docs/api/LIFUDFU.md` — the console update/migration paths consume
images produced here and use `validate_signed_image` as their pre-flash check.
