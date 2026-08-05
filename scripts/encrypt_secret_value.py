from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.security.secret_crypto import encrypt_text


def main() -> None:
    print("FundCraft secret encryptor")
    print("This script encrypts exactly one value per run.")

    plain_text = input("请输入需要加密的明文内容: ").strip()
    if not plain_text:
        raise ValueError("明文内容不能为空。")

    password = input("请输入加密口令: ").strip()
    if not password:
        raise ValueError("加密口令不能为空。")

    ciphertext = encrypt_text(plain_text, password)
    print()
    print("加密结果如下，可直接写入 secrets.toml：")
    print(ciphertext)


if __name__ == "__main__":
    main()