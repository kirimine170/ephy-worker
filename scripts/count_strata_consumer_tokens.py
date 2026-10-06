"""Count a full OpenAI chat payload with pinned existing Strata text components.

Run under the existing Strata Python environment. Imports only its frontend and
tokenizer; never imports the server, launches an engine, or contacts a service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def count(spec: dict, raw: bytes) -> dict:
    root = Path(spec["tokenizer_root"])
    frontend = Path(spec["frontend_root"])
    mandatory = [Path(spec["configuration"]), frontend / "serve/frontend.py",
                 frontend / "tools/strata_tokenizer.py", Path(sys.executable)]
    mandatory += [root / name for name in (
        "vocab.json", "merges.txt", "token_type.json", "chat_template.jinja", "tokenizer.json")]
    pinned_paths = {Path(name).resolve() for name in spec["pins"]}
    if not {p.resolve() for p in mandatory} <= pinned_paths:
        raise ValueError("Missing deployment/tokenizer implementation pins")
    for name, expected in spec["pins"].items():
        if digest(Path(name).read_bytes()) != expected:
            raise ValueError("Frozen tokenizer/runtime bytes changed")
    config = json.loads(Path(spec["configuration"]).read_text(encoding="utf-8"))
    if (
        config["model_name"] != spec["model_id"]
        or Path(config["tokenizer"]).resolve() != Path(spec["tokenizer_root"]).resolve()
    ):
        raise ValueError("Tokenizer is not from the frozen deployment configuration")
    request = json.loads(raw)
    if request.get("response_format") or request.get("strata_mcp"):
        raise ValueError("Structured output and server MCP augmentation are outside this profile")
    if request.get("reasoning_effort") != "none":
        raise ValueError("Only explicit thinking-off payloads are measured")
    sys.path.insert(0, str(Path(spec["frontend_root"])))
    sys.path.insert(0, str(Path(spec["frontend_root"]) / "tools"))
    import strata_tokenizer as ST
    from serve import frontend as loaded_frontend
    from serve.frontend import ChatTemplate, openai_to_messages

    if (Path(ST.__file__).resolve() != (frontend / "tools/strata_tokenizer.py").resolve()
            or Path(loaded_frontend.__file__).resolve() != (frontend / "serve/frontend.py").resolve()):
        raise ValueError("Imported a different tokenizer/frontend")
    import jinja2
    import markupsafe
    import regex

    for package in (jinja2, regex, markupsafe):
        closure = {p.resolve() for p in Path(package.__file__).parent.rglob("*")
                   if p.is_file() and p.suffix in (".py", ".pyd", ".dll")}
        if not closure <= pinned_paths:
            raise ValueError("Missing tokenizer dependency source pins")

    vocab = json.loads((root / "vocab.json").read_text(encoding="utf-8"))
    tokens = [None] * len(vocab)
    for text, number in vocab.items():
        tokens[number] = text
    merges = (root / "merges.txt").read_text(encoding="utf-8").split("\n")
    types = json.loads((root / "token_type.json").read_text(encoding="utf-8"))
    tokenizer = ST.Tokenizer(tokens, merges, types)
    messages, tools, kwargs = openai_to_messages(request)
    rendered = ChatTemplate(root / "chat_template.jinja").render(messages, tools=tools, **kwargs)
    result = {
        "payload_sha256": digest(raw),
        "rendered_sha256": digest(rendered.encode("utf-8")),
        "input_tokens": len(tokenizer.encode(rendered, parse_special=True)),
        "model_id": spec["model_id"],
        "spec_sha256": digest(Path(spec["spec_path"]).read_bytes()),
        "model_generation_requests": 0,
    }
    for name, expected in spec["pins"].items():
        if digest(Path(name).read_bytes()) != expected:
            raise ValueError("Tokenizer/runtime changed while measuring")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    spec["spec_path"] = str(args.spec)
    print(json.dumps(count(spec, sys.stdin.buffer.read()), separators=(",", ":")))


if __name__ == "__main__":
    main()
