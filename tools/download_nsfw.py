"""Скачать модель для проверки аватарок в tools/models/nsfw.

Запуск:  python tools/download_nsfw.py

Берём готовый ONNX: у этой модели он выложен автором, поэтому экспортировать
самим (как rubert) ничего не надо — ни torch, ни transformers не понадобятся.

Модель: AdamCodd/vit-base-nsfw-detector, лицензия Apache-2.0. Два ответа —
sfw и nsfw. Восьмибитная версия весит около 84 МБ; полная 328 МБ качества
почти не добавляет, а память ест втрое.

Файлов нет — проверка аватарок молча выключена, бот работает как обычно.
"""
import os
import urllib.request

REPO = "AdamCodd/vit-base-nsfw-detector"
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "nsfw")
# (что качаем, под каким именем кладём)
FILES = (("onnx/model_int8.onnx", "model.onnx"),
         ("config.json", "config.json"),
         ("preprocessor_config.json", "preprocessor_config.json"))


def main() -> None:
    os.makedirs(DEST, exist_ok=True)
    for remote, name in FILES:
        path = os.path.join(DEST, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            print(f"уже есть  {name}")
            continue
        url = f"https://huggingface.co/{REPO}/resolve/main/{remote}"
        print(f"качаю     {name} …", end=" ", flush=True)
        urllib.request.urlretrieve(url, path)
        print(f"{os.path.getsize(path) // 1024} КБ")
    print(f"\nготово: {DEST}")


if __name__ == "__main__":
    main()
