"""Скачать rubert-tiny2 в tools/models. Сами веса в репозиторий не кладём.

Запуск:  python tools/download_model.py

Модель маленькая (около 118 МБ) и нужна только для нейрофильтра. Если файлов
нет, фильтр молча остаётся в режиме сбора данных, бот работает как обычно.
"""
import os
import urllib.request

REPO = "cointegrated/rubert-tiny2"
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "rubert-tiny2")
FILES = ("config.json", "tokenizer.json", "tokenizer_config.json",
         "special_tokens_map.json", "vocab.txt", "model.safetensors")


def main() -> None:
    os.makedirs(DEST, exist_ok=True)
    for name in FILES:
        path = os.path.join(DEST, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            print(f"уже есть  {name}")
            continue
        url = f"https://huggingface.co/{REPO}/resolve/main/{name}"
        print(f"качаю     {name} …", end=" ", flush=True)
        urllib.request.urlretrieve(url, path)
        print(f"{os.path.getsize(path) // 1024} КБ")
    print(f"\nготово: {DEST}")


if __name__ == "__main__":
    main()
