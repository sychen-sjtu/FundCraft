from __future__ import annotations

from getpass import getpass

from src.security.secret_crypto import encrypt_text


def main() -> None:
    print("请输入需要加密的明文内容，然后回车：")
    plain_text = input().strip()
    if not plain_text:
        raise SystemExit("明文不能为空，已退出。")

    password = getpass("请输入加密口令：")
    confirm_password = getpass("请再次输入加密口令：")
    if password != confirm_password:
        raise SystemExit("两次口令不一致，已退出。")

    print("\n加密结果如下，请复制到 secrets.toml：")
    print(encrypt_text(plain_text, password))


if __name__ == "__main__":
    main()