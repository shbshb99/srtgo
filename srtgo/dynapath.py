"""DynaPath anti-macro token for the Korail mobile API.

Vendored from yakisoba0728/korail-mobile-api
(https://github.com/yakisoba0728/korail-mobile-api), Copyright (c) 2026
yakisoba0728, licensed under the Apache License, Version 2.0
(http://www.apache.org/licenses/LICENSE-2.0). See NOTICE in the srtgo
repository root for the upstream attribution notice.

Adapted for srtgo (changes per Apache-2.0 4(b)): trimmed to the pure
token-generation functions srtgo needs to attach the `x-dynapath-m-token`
header to login/search/reserve requests; dropped the upstream package's
pluggable client/config wrapper classes (DynapathConfig,
DynapathTokenGenerator, DynapathRequestContext) and inlined the handful of
constants those functions depend on. The algorithm itself is unchanged from
upstream, which reverse-engineered it from the decompiled Korail Android app
(`com.korail.talk`, DynaPath SDK classes `b/C1229b.java`,
`B/AbstractC1228a.java`).

Korail's server disguises a rejection here as a generic "please update your
app" (MACRO ERROR) message, which is easy to misdiagnose as a stale
`Version`/`User-Agent` value (see srtgo issue history) rather than a missing
token.
"""
from __future__ import annotations

import random
import string
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote_plus

DYNAPATH_HEADER_NAME = "x-dynapath-m-token"

# DynaPath `dm`/`os` fields = Build.MODEL / Build.VERSION.RELEASE
# (b/C1229b.java:128-132). Use the generic values the real app's platform
# default resolves to rather than inventing a specific handset model.
KORAIL_DEFAULT_DEVICE_NAME = "Android"
KORAIL_DEFAULT_ANDROID_OS_RELEASE = "15"

KORAIL_DYNAPATH_APP_ID = "com.korail.talk"
KORAIL_DYNAPATH_OS_TYPE = "Android"
KORAIL_DYNAPATH_SDK_VERSION = "v1.0.3"
# SHA-256 of the APK signing cert (verified from META-INF/BNDLTOOL.RSA) --
# constant for every install of the real app, truncated to 32 chars by
# AbstractC5987i.java and wrapped in ArrayList.toString().
KORAIL_DYNAPATH_SIGNING_CERT_SHA256 = (
    "38ff229cb34c7dda8e28220a2d750cceec28db661a36d95ad92d82f6d3c618f9"
)
KORAIL_DYNAPATH_APP_SIGNATURE_HASH = KORAIL_DYNAPATH_SIGNING_CERT_SHA256[:32]
KORAIL_DYNAPATH_AS_VALUE = f"[{KORAIL_DYNAPATH_APP_SIGNATURE_HASH}]"


def build_dalvik_user_agent(*, os_release: str, device_model: str) -> str:
    """Build the User-Agent the way the app's networking stack does.

    com.korail.talk does not hardcode a UA -- it runs Retrofit v1 over
    HttpURLConnection (ExecuteDao.java:7-11), so the platform default Dalvik
    string goes out. No trailing `Build/<id>` is added: inventing one for a
    device model that doesn't match would be an unverifiable claim, and a UA
    claiming one handset while the DynaPath token claims another is a
    mismatch the server can check.
    """
    return f"Dalvik/2.1.0 (Linux; U; Android {os_release}; {device_model})"


DYNAPATH_BASE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
DYNAPATH_TABLE_INDEX = 1
# Nonce alphabet from b/C1229b.java:164 (smali b.1/b.smali:549):
# CharsKt.random("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
DYNAPATH_RANDOM_ALPHABET = (
    string.ascii_lowercase + string.ascii_uppercase + string.digits
)
DYNAPATH_DEFAULT_I8 = 161
DYNAPATH_DEFAULT_I9 = 30
DYNAPATH_DEFAULT_I10 = 2


def _prime_table(count: int = 100) -> list[int]:
    primes: list[int] = []
    candidate = 2
    while len(primes) < count + 1:
        is_prime = True
        for prime in primes:
            if prime * prime > candidate:
                break
            if candidate % prime == 0:
                is_prime = False
                break
        if is_prime:
            primes.append(candidate)
        candidate += 1
    return primes[1:]


DYNAPATH_PRIMES = tuple(_prime_table())


def _sdk_permute_alphabet(value: str, multiplier: int, step: int) -> str:
    length = len(value)
    block_size = 1
    for prime in DYNAPATH_PRIMES:
        if prime <= length:
            block_size = prime
        else:
            break

    counts = [0] * block_size
    chars = [""] * block_size
    factor = 1
    for idx in range(block_size):
        target = ((factor % block_size) * step) % block_size
        counts[target] += 1
        if counts[target] == 1:
            chars[idx] = value[target]
        factor *= multiplier

    encoded: list[str] = []
    missing: list[str] = []
    for idx, char in enumerate(chars):
        if char:
            encoded.append(char)
            continue
        for missing_idx in range(block_size):
            if counts[missing_idx] == 0:
                replacement = value[missing_idx]
                chars[idx] = replacement
                missing.append(replacement)
                counts[missing_idx] = 1
                break

    while block_size < length:
        missing.append(value[block_size])
        block_size += 1

    missing_text = "".join(missing)
    if len(missing_text) < DYNAPATH_PRIMES[0]:
        return "".join(encoded) + missing_text
    return "".join(encoded) + _sdk_permute_alphabet(missing_text, multiplier, step)


def generate_dynapath_encoding_table(index: int = DYNAPATH_TABLE_INDEX) -> str:
    multiplier = DYNAPATH_PRIMES[index % 29]
    step = DYNAPATH_PRIMES[(index // 29) % 29]
    return _sdk_permute_alphabet(DYNAPATH_BASE_ALPHABET, multiplier, step)


DYNAPATH_ENCODING_TABLE = generate_dynapath_encoding_table(DYNAPATH_TABLE_INDEX)


def build_dynapath_prefix(
    *,
    table: str,
    table_index: int = DYNAPATH_TABLE_INDEX,
    i11: int = 2,
    i12: int = 30,
) -> str:
    return f"{chr(table_index + 97)}{table[2]}{table[37]}{table[i11]}{table[i12 - 1]}"


@dataclass(frozen=True)
class DynapathTokenSettings:
    device_id: str
    app_start_ts: str
    os_version: str = KORAIL_DEFAULT_ANDROID_OS_RELEASE
    device_model: str = KORAIL_DEFAULT_DEVICE_NAME
    as_value: str = KORAIL_DYNAPATH_AS_VALUE
    app_id: str = KORAIL_DYNAPATH_APP_ID
    os_type: str = KORAIL_DYNAPATH_OS_TYPE
    sdk_version: str = KORAIL_DYNAPATH_SDK_VERSION
    table_index: int = DYNAPATH_TABLE_INDEX
    table: str = DYNAPATH_ENCODING_TABLE
    i8: int = DYNAPATH_DEFAULT_I8
    i9: int = DYNAPATH_DEFAULT_I9
    i10: int = DYNAPATH_DEFAULT_I10
    secure_user: bool = False
    debug: bool = False
    emulator: bool = False
    hooked: bool = False


def generate_dynapath_device_id() -> str:
    """Synthetic Settings.Secure.ANDROID_ID (AbstractC1228a.java:16).

    64-bit lowercase hex, 16 chars. Generate once per Korail client instance
    and keep it stable for that instance's requests.
    """
    return uuid.uuid4().hex[:16]


def _timestamp_ms() -> int:
    return int(time.time() * 1000)


def _random_text() -> str:
    return "".join(random.choices(DYNAPATH_RANDOM_ALPHABET, k=4))


def string_to_xa1s(data: str) -> list[int]:
    result: list[int] = []
    for ch in data:
        cp = ord(ch)
        if cp < 128:
            result.append(cp)
        elif cp < 2048:
            result.append(128 | ((cp >> 7) & 15))
            result.append(cp & 127)
        elif cp >= 262144:
            result.append(160)
            result.append((cp >> 14) & 127)
            result.append((cp >> 7) & 127)
            result.append(cp & 127)
        elif (63488 & cp) != 55296:
            result.append(((cp >> 14) & 15) | 144)
            result.append((cp >> 7) & 127)
            result.append(cp & 127)
    return result


def make_dynapath_key(key: str) -> int:
    value = 0
    for ch in key:
        cp = ord(ch)
        bit = 32768
        for _ in range(16):
            if bit & cp:
                break
            bit >>= 1
        value = (value * (bit << 1)) + cp
    return value


def _pick_table_char(base_table: str, remainder: int, used: str) -> str:
    count = 0
    for ch in base_table:
        if ch not in used:
            if count == remainder:
                return ch
            count += 1
    return " "


def make_encode_table(num: int, encode_size: int, base_table: str) -> str:
    result = ""
    temp = num
    for i in range(encode_size):
        divisor = encode_size - i
        remainder = temp % divisor
        result += _pick_table_char(base_table, remainder, result)
        temp //= divisor
    return result


def encode_normal_be(data: str, table: str, *, i8: int = 161, i9: int = 30, i10: int = 2) -> str:
    bytes_like = string_to_xa1s(data)
    out: list[str] = []
    arr = [0] * (i10 + 1)

    idx = 0
    remain = len(bytes_like) % i10
    full_len = len(bytes_like) - remain

    while idx < full_len:
        val = 0
        for _ in range(i10):
            val = (val * i8) + bytes_like[idx]
            idx += 1
        for i in range(i10 + 1):
            arr[i] = val % i9
            val //= i9
        for i in range(i10, -1, -1):
            out.append(table[arr[i]])

    if remain > 0:
        val = 0
        for _ in range(remain):
            val = (val * i8) + bytes_like[idx]
            idx += 1
        for i in range(remain + 1):
            arr[i] = val % i9
            val //= i9
        while remain >= 0:
            out.append(table[arr[remain]])
            remain -= 1

    return "".join(out)


def java_urlencode(value: str) -> str:
    return quote_plus(value, safe="*-._").replace("~", "%7E")


def _java_form_encode(fields: list[tuple[str, str]]) -> str:
    return "&".join(f"{java_urlencode(key)}={java_urlencode(value)}" for key, value in fields)


def generate_dynapath_token(
    settings: DynapathTokenSettings,
    *,
    timestamp_ms: int | None = None,
    random_text: str | None = None,
) -> str:
    ts = _timestamp_ms() if timestamp_ms is None else timestamp_ms
    rand = _random_text() if random_text is None else random_text
    fields = [
        ("ai", settings.app_id),
        ("di", settings.device_id),
        ("as", settings.as_value),
        ("su", str(settings.secure_user).lower()),
        ("dbg", str(settings.debug).lower()),
        ("emu", str(settings.emulator).lower()),
        ("hk", str(settings.hooked).lower()),
        ("it", settings.app_start_ts),
        ("ts", str(ts)),
        # The app sends `rt` as an array of inter-request delays and omits
        # the field entirely when none have accumulated yet
        # (B/C1229b.java:118-127). A fresh client always takes that path, so
        # a fixed "0" matches what the server actually accepts here; `rt` is
        # not used in key derivation (dyn_key is sv+rand+ts).
        ("rt", "0"),
        ("os", settings.os_version),
        ("dm", settings.device_model),
        ("st", settings.os_type),
        ("sv", settings.sdk_version),
    ]
    payload = _java_form_encode(fields)
    dyn_key = f"{settings.sdk_version}+{rand}+{ts}"
    encoded_key = encode_normal_be(
        dyn_key,
        settings.table,
        i8=settings.i8,
        i9=settings.i9,
        i10=settings.i10,
    )
    custom_table = make_encode_table(
        make_dynapath_key(dyn_key),
        settings.i9,
        settings.table,
    )
    encoded_body = encode_normal_be(
        payload,
        custom_table,
        i8=settings.i8,
        i9=settings.i9,
        i10=settings.i10,
    )
    prefix = build_dynapath_prefix(
        table=settings.table,
        table_index=settings.table_index,
        i11=settings.i10,
        i12=settings.i9,
    )
    return f"{prefix}{settings.table[len(encoded_key)]}{encoded_key}{encoded_body}"
