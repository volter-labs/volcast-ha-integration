"""Czekanie na potwierdzenie parowania: statusy terminalne, termin, błędy sieci."""
import asyncio

from custom_components.volcast.cloud.client import PairingSession, PollResult
from custom_components.volcast.pairing import PairingPoller

S = PairingSession("s1", "p", "https://volcast.app/connect?s=s1", "t")


class Client:
    def __init__(self, *results):
        self.results, self.n = list(results), 0

    async def async_poll(self, s):
        self.n += 1
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def run(client, sleeps=None, **kw):
    clock = Clock()

    async def sleep(s):
        if sleeps is not None:
            sleeps.append(s)
        clock.t += s
    return asyncio.run(PairingPoller(client, S, sleep=sleep, clock=clock, **kw).async_wait())


def test_pending_then_confirmed():
    r = run(Client(PollResult("pending"), PollResult("pending"), PollResult("confirmed", api_key="vk_x")))
    assert r.status == "confirmed"


def test_terminal_statuses_return_immediately():
    for st in ("expired", "gone", "disabled"):
        c = Client(PollResult(st))
        assert run(c).status == st and c.n == 1


def test_consumed_while_waiting_is_failed_not_wait():
    """Klucz poszedł w zgubionej odpowiedzi — dalsze czekanie nic nie da."""
    c = Client(PollResult("pending"), PollResult("consumed"))
    assert run(c).status == "consumed" and c.n == 2


def test_deadline_gives_expired():
    assert run(Client(PollResult("pending")), deadline_s=10.0, interval_s=3.0).status == "expired"


def test_errors_back_off_and_give_up():
    c = Client(PollResult("error"))
    sleeps: list[float] = []
    assert run(c, sleeps, max_errors=5).status == "error" and c.n == 5
    assert sleeps == sorted(sleeps) and sleeps[0] > 3.0 and max(sleeps) <= 30.0


def test_error_counter_resets_after_success():
    c = Client(*([PollResult("error")] * 4 + [PollResult("pending")] + [PollResult("error")] * 4
                 + [PollResult("confirmed")]))
    assert run(c, max_errors=5).status == "confirmed"


def test_errors_stop_at_deadline():
    assert run(Client(PollResult("error")), deadline_s=20.0, max_errors=1000).status == "expired"
