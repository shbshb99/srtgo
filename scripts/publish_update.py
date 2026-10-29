"""새 버전을 자동 업데이트 채널(updates/)에 올린다.

사용: python scripts/publish_update.py 2.2.2+ktx.12 "바뀐 점 (오너 폰에 보임)"
휠을 빌드해 updates/ 에 넣고 latest.json(버전, 파일 이름, SHA256, 바뀐 점)을 갱신한다.
그다음 커밋·푸시하면 운영PC 워치독이 30분 안에 알아채고, 오너가 텔레그램에서
[설치]를 누르면 설치한다 (자동 모드면 바로 설치).
"""

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
UPDATES = ROOT / "updates"


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    version = sys.argv[1]
    notes = sys.argv[2] if len(sys.argv) == 3 else ""
    with tempfile.TemporaryDirectory() as out:
        env = dict(os.environ, SETUPTOOLS_SCM_PRETEND_VERSION=version)
        subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", out], cwd=ROOT, env=env, check=True)
        [wheel] = pathlib.Path(out).glob("*.whl")
        expected = f"srtgo-{version}-py3-none-any.whl"
        if wheel.name != expected:
            sys.exit(f"빌드된 파일 이름이 다릅니다: {wheel.name} (기대: {expected})")
        UPDATES.mkdir(exist_ok=True)
        for old in UPDATES.glob("srtgo-*.whl"):
            old.unlink()  # 저장소가 커지지 않게 최신 하나만 둔다
        shutil.copy(wheel, UPDATES / wheel.name)
    data = (UPDATES / expected).read_bytes()
    manifest = {"version": version, "wheel": expected, "sha256": hashlib.sha256(data).hexdigest(), "notes": notes}
    (UPDATES / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
