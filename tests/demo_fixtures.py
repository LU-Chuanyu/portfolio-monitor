"""Build synthetic history for tests without contacting an exchange."""

from datetime import date
import json
from pathlib import Path

from scripts.generate_demo_data import build_demo_generation


PORTFOLIO_PATH = Path(__file__).resolve().parents[1] / 'examples' / 'portfolio.json'


def demo_portfolio():
    return json.loads(PORTFOLIO_PATH.read_text(encoding='utf-8'))


def demo_generation(days=10):
    return build_demo_generation(demo_portfolio(), end_date=date(2025, 12, 31), days=days)
