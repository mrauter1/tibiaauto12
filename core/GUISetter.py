"""Tk variable loading and atomic GUI preference updates."""
from __future__ import annotations

import threading
import tkinter as tk
from typing import Any

from conf.conf_manager import ConfManager


def GetData():
    return ConfManager.get("GUILoader.json")


def check_gui(gui_changes, init, get, name):
    if get != init:
        gui_changes.append((name, get))


class GUISetter:
    def __init__(self, Locate):
        self.Locate = Locate
        self.Variables = Variables(Locate)
        self.SetVariables = SetVariables(Locate)


class Variables:
    def __init__(self, Locate):
        self.Locate = Locate

    def Bool(self, VarName):
        initiated = SetVariables(self.Locate).GetBoolVar(VarName)
        variable = tk.BooleanVar(value=initiated)
        return variable, initiated

    def Str(self, VarName):
        initiated = SetVariables(self.Locate).GetVar(VarName)
        variable = tk.StringVar(value=initiated)
        return variable, initiated

    def Int(self, VarName):
        initiated = SetVariables(self.Locate).GetVar(VarName)
        variable = tk.IntVar(value=initiated)
        return variable, initiated

    def array(self, name):
        initiated = SetVariables(self.Locate).GetVar(name)
        return list(initiated), initiated


class SetVariables:
    def __init__(self, Locate):
        self.Locate = Locate

    def GetBoolVar(self, VarName):
        return bool(GetData()["Loaders"][self.Locate][VarName])

    def GetVar(self, VarName):
        return GetData()["Loaders"][self.Locate][VarName]

    def _set(self, Name: str, Value: Any):
        locate = self.Locate

        def update(data):
            data["Loaders"][locate][Name] = Value

        return ConfManager.update("GUILoader.json", update)

    def SetVar(self, Name, Value, widget=None):
        """Save atomically; schedule through Tk when invoked by a worker."""
        if threading.current_thread() is threading.main_thread():
            return self._set(Name, Value)
        if widget is None:
            # File locking makes the write safe even without a live window.
            return self._set(Name, Value)
        widget.after(0, self._set, Name, Value)
        return None

    def SetVarOnMainThread(self, widget, Name, Value):
        return self.SetVar(Name, Value, widget=widget)
