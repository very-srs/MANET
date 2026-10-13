#!/usr/bin/env python3
"""Keep every manage tab pane visible on its own when its tab is selected."""

from html.parser import HTMLParser
import unittest
from unittest.mock import patch

import manet_manage as manage

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'source', 'track', 'wbr'}


class PaneParser(HTMLParser):
    """Record each tab pane's id and the tab panes that enclose it."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.panes = {}

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        a = dict(attrs)
        pane = a.get('id') if 'tab-pane' in (a.get('class') or '').split() else None
        if pane:
            self.panes[pane] = [p for _, p in self.stack if p]
        self.stack.append((tag, pane))

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break


class TabPaneTests(unittest.TestCase):
    def setUp(self):
        with patch.object(manage, 'get_my_hostname', return_value='mesh-test'), \
             patch.object(manage, 'get_my_ip', return_value='10.0.0.1'), \
             patch.object(manage, 'logo_asset_token', return_value='1'):
            html = manage.render_dashboard()
        self.parser = PaneParser()
        self.parser.feed(html)

    def test_every_valid_tab_has_a_pane(self):
        for tab in ('topology', 'radio', 'measure', 'sessions', 'uplink',
                    'voice', 'config'):
            self.assertIn('tab-' + tab, self.parser.panes)

    def test_no_pane_is_nested_in_another(self):
        # showTab hides every other pane, so a nested pane never shows.
        for pane, parents in self.parser.panes.items():
            self.assertEqual(parents, [], pane + ' is inside ' + ', '.join(parents))


if __name__ == '__main__':
    unittest.main()
