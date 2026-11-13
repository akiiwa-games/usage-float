"""Release Tcl interpreters on the thread that created them.

A Tk root that a test leaves in cyclic garbage is finalized by whichever thread next runs the collector. From a worker
thread Tcl aborts the whole process ("Tcl_AsyncDelete: async handler deleted by the wrong thread", exit 0x80000003),
which showed up as an occasional crash in a later test that only used a thread pool. So a test that created a Tk root
is collected on the main thread before the next test starts.
"""
import gc
import tkinter

import pytest


@pytest.fixture(autouse=True)
def release_tk_roots_on_the_main_thread(monkeypatch):
    created = []
    original = tkinter.Tk.__init__

    def tracked(self, *args, **kwargs):
        created.append(self.__class__.__name__)
        original(self, *args, **kwargs)

    monkeypatch.setattr(tkinter.Tk, "__init__", tracked)
    yield
    if created:
        gc.collect()
