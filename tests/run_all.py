"""자체 점검 전부 실행 — `python tests/run_all.py`.

레포에 pytest 설정이 없다(requirements.txt 에도 없다). tests/ 의 파일들은 각각
`python tests/<파일>.py` 로 직접 도는 스크립트라, 이 러너가 그 셋을 차례로 돌리고
결과를 모아 준다. 하나라도 실패하면 종료 코드 1.

각 점검을 별도 프로세스로 띄운다 — 어떤 점검은 모듈 속성을 갈아끼우므로(스텁·세션 대역)
한 프로세스에서 몰아 돌리면 서로 오염된다.
"""
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def main() -> int:
    scripts = sorted(p for p in TESTS_DIR.glob("test_*.py"))
    if not scripts:
        print("돌릴 점검이 없다")
        return 1

    failed = []
    for script in scripts:
        print(f"─── {script.name}")
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(TESTS_DIR.parent),
            capture_output=True,
            text=True,
        )
        # 설정 로딩 로그 등 잡음은 걷어내고 점검 출력만 보여 준다.
        for line in (result.stdout + result.stderr).splitlines():
            if "INFO [config" in line:
                continue
            print(f"  {line}")
        if result.returncode != 0:
            failed.append(script.name)

    print()
    if failed:
        print(f"실패 {len(failed)}/{len(scripts)}: {', '.join(failed)}")
        return 1
    print(f"전체 통과 ({len(scripts)}개)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
