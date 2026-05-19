import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import a as quiz
    from config import get_missing_auth_fields, get_runtime_config
else:  # pragma: no cover
    from . import a as quiz
    from .config import get_missing_auth_fields, get_runtime_config

if __name__ == "__main__":
    task_id = int(sys.argv[1])
    release_id = int(sys.argv[2])
    config = get_runtime_config()
    missing = get_missing_auth_fields(config)
    if missing:
        raise SystemExit(f"缺少必要配置: {', '.join(missing)}")
    c = quiz.Client(config["USERTOKEN"], config["ABC"], config["AUTH_V"])
    quiz.run_full(c, task_id=task_id, release_id=release_id)
