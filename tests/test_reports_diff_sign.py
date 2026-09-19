"""weekly/monthly レポートの前期比表示: 支出減少時に符号が消えないことのテスト。"""
from datetime import date

import reports


def test_weekly_report_shows_minus_sign_when_spending_decreased(monkeypatch):
    captured = {}
    monkeypatch.setattr(reports.notifier, 'push', lambda text: captured.setdefault('text', text))
    monkeypatch.setattr(reports, 'generate_weekly', lambda week_start=None: {
        'week_start': date(2026, 5, 4),
        'week_total': 8000,
        'prev_week_total': 10000,
        'days': [],
    })

    reports.send_weekly_report()

    assert '先週比  -¥2,000 (-20.0%)' in captured['text']


def test_weekly_report_shows_plus_sign_when_spending_increased(monkeypatch):
    captured = {}
    monkeypatch.setattr(reports.notifier, 'push', lambda text: captured.setdefault('text', text))
    monkeypatch.setattr(reports, 'generate_weekly', lambda week_start=None: {
        'week_start': date(2026, 5, 4),
        'week_total': 12000,
        'prev_week_total': 10000,
        'days': [],
    })

    reports.send_weekly_report()

    assert '+¥2,000' in captured['text']


def test_monthly_report_shows_minus_sign_when_spending_decreased(monkeypatch):
    captured = {}
    monkeypatch.setattr(reports.notifier, 'push', lambda text: captured.setdefault('text', text))
    monkeypatch.setattr(reports, 'generate_monthly', lambda year=None, month=None: {
        'year': 2026, 'month': 5,
        'total': 30000,
        'prev_total': 50000,
        'top_stores': [],
        'by_type': {},
    })

    reports.send_monthly_report()

    assert '先月比  -¥20,000 (-40.0%)' in captured['text']
