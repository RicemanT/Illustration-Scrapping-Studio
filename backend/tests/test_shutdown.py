import asyncio
import signal
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.routes import settings


class ShutdownRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_confirmation_and_signals_only_after_responding(self):
        with self.assertRaises(ValidationError):
            settings.ShutdownRequest(confirm=False)
        with patch.object(settings.signal, 'raise_signal') as raise_signal, patch('app.services.diagnostics.emit'):
            result = await settings.shutdown_server(settings.ShutdownRequest(confirm=True))
            self.assertEqual(result, {'status': 'stopping'})
            raise_signal.assert_not_called()
            await asyncio.sleep(0.8)
            raise_signal.assert_called_once_with(signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()
