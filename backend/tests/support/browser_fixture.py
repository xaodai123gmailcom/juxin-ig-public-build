"""Offline browser fixture launch policy shared by Instagram browser tests."""
import os


def browser_fixture_launch_options(*, windows=None):
    """Match headed Windows Chrome and the product's background policy."""
    windows = os.name == 'nt' if windows is None else windows
    return {'headless': not windows, 'args': [
        '--disable-background-mode', '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows', '--disable-renderer-backgrounding',
    ]}
