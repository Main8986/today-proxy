#!/usr/bin/env python3
"""Today 账号登录 CLI（跑在 VPS 上，也是 install.sh 调用的入口）。

    python3 login.py +8613800000000            # 发短信验证码
    python3 login.py +8613800000000 123456     # 换 session token，写入 data/session.txt
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import send_otp, verify_otp

ROOT = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(ROOT, "data", "session.txt")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    phone = sys.argv[1]
    if len(sys.argv) == 2:
        st, out = send_otp(phone)
        print("HTTP", st, out[:300])
        return 0 if st == 200 else 2
    tok, user = verify_otp(phone, sys.argv[2])
    if not tok:
        print("verify failed:", user)
        return 2
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
    with open(SESSION_FILE, "a") as f:
        f.write(tok + "\n")
    print("OK  user:", (user or {}).get("email") or (user or {}).get("id"))
    print("session token saved ->", SESSION_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
