"""Разовый экспорт rubert-tiny2 в ONNX — чтобы бот считал эмбеддинги без torch.

Зачем: сам torch весит сотни мегабайт, тащить его в образ бота ради модели
на 118 МБ глупо. Экспортируем один раз здесь, а контейнеру хватит onnxruntime
и tokenizers — это десятки мегабайт.

Что нужно на этой машине (в контейнер ничего из этого не поедет):

    pip install torch transformers onnx

Запуск:  python tools/export_onnx.py
Результат: tools/models/rubert-tiny2/model.onnx
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "models", "rubert-tiny2")
OUT = os.path.join(SRC, "model.onnx")


def main() -> None:
    if not os.path.exists(os.path.join(SRC, "config.json")):
        sys.exit("сначала скачайте модель: python tools/download_model.py")
    try:
        import torch
        from transformers import AutoModel
    except ImportError:
        sys.exit("нужны torch и transformers:  pip install torch transformers onnx")

    model = AutoModel.from_pretrained(SRC)
    model.eval()

    # фиктивный вход: две короткие фразы, чтобы в графе появились обе оси
    ids = torch.tensor([[2, 100, 200, 3], [2, 300, 3, 0]], dtype=torch.long)
    mask = torch.tensor([[1, 1, 1, 1], [1, 1, 1, 0]], dtype=torch.long)
    types = torch.zeros_like(ids)

    dynamic = {name: {0: "batch", 1: "seq"} for name in
               ("input_ids", "attention_mask", "token_type_ids")}
    dynamic["last_hidden_state"] = {0: "batch", 1: "seq"}

    print("экспортирую…")
    torch.onnx.export(
        model, (ids, mask, types), OUT,
        input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["last_hidden_state"],
        dynamic_axes=dynamic, opset_version=14, do_constant_folding=True,
    )
    print(f"готово: {OUT} ({os.path.getsize(OUT) // 1024 // 1024} МБ)")
    print("теперь боту нужны только:  pip install onnxruntime tokenizers")


if __name__ == "__main__":
    main()
