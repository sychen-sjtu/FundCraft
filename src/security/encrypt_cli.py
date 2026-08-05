"""正式单值加密交互工具：一次加密一个明文值，输出可直接写入 TOML 的密文。"""

from __future__ import annotations

from src.security.secret_crypto import encrypt_text


def run_encrypt_interactive() -> str:
    """交互式加密一个值，返回密文。"""
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
    return ciphertext


if __name__ == "__main__":
    run_encrypt_interactive()
