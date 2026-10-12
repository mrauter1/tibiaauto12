"""Batch widget state changes without retaining destroyed widgets."""
from __future__ import annotations

import weakref

_ComponentsForEnable: list[weakref.ReferenceType] = []
_ComponentsForDisable: list[weakref.ReferenceType] = []
# Compatibility aliases. Lists contain weak references rather than widgets.
ComponentsForEnable = _ComponentsForEnable
ComponentsForDisable = _ComponentsForDisable


def _remember(collection, component):
    try:
        collection.append(weakref.ref(component))
    except TypeError:
        # Tk widgets support weak references; fail clearly for invalid callers.
        raise TypeError("GUI state target must be a live Tk widget")


def Enable(Component):
    _remember(_ComponentsForEnable, Component)


def Disable(Component):
    _remember(_ComponentsForDisable, Component)


def _apply(collection, state):
    for reference in collection:
        widget = reference()
        if widget is None:
            continue
        try:
            if widget.winfo_exists():
                widget.configure(state=state)
        except Exception:
            # A widget may be destroyed between the existence check and update.
            continue
    collection.clear()


def ComponentsToDisable():
    _apply(_ComponentsForDisable, "disabled")


def ComponentsToEnable():
    _apply(_ComponentsForEnable, "normal")


def ExecGUITrigger():
    ComponentsToEnable()
    ComponentsToDisable()
