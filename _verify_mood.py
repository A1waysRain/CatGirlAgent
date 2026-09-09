"""验证回复情绪规则始终输出 pet_config.json 支持的标准键。

用法：cd Cat_Girl && .venv/Scripts/python.exe _verify_mood.py
"""

from backend.mood import infer_mood


def check(name: str, actual: str, expected: str) -> int:
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'} {name}: {actual!r}")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    failures += check("惊讶映射到 excited", infer_mood("天啊，太惊讶了喵"), "excited")
    failures += check("困倦映射到 sleep", infer_mood("本喵好困，想睡觉了"), "sleep")
    failures += check("委屈映射到 sad", infer_mood("呜呜，好委屈"), "sad")
    failures += check("默认映射到 happy", infer_mood("今天就这样吧"), "happy")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
