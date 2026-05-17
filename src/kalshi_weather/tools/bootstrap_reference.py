from __future__ import annotations

from kalshi_weather.storage.reference_registry import FileReferenceRegistry


def main() -> None:
    registry = FileReferenceRegistry()
    path = registry.write_seed(registry.default_seed())
    print(path)


if __name__ == "__main__":
    main()
