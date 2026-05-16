from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bitrix_mcp.agent import answer_question
from bitrix_mcp.config import Settings
from bitrix_mcp.server import configure_logfire


def read_question() -> str:
    if len(sys.argv) > 1:
        return ' '.join(sys.argv[1:]).strip()
    return input('Question: ').strip()


async def async_main() -> int:
    settings = Settings.from_env()

    try:
        settings.validate_for_run()
    except RuntimeError as exc:
        print(f'Configuration error: {exc}', file=sys.stderr)
        return 1

    configure_logfire(settings)

    question = read_question()
    if not question:
        print('Question must not be empty.', file=sys.stderr)
        return 1

    answer = await answer_question(question, settings)
    print(answer)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == '__main__':
    main()
