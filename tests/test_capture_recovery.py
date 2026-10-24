from pathlib import Path
import time,threading
import numpy as np
import pytest
from core.perception import CaptureError,Frame
from tibia_client.capture import CaptureProcess,TransientCaptureError,_retryable_capture_error

class FailOnce:
    def __init__(self,path):self.path=str(path)
    def __call__(self):
        if not Path(self.path).exists():
            Path(self.path).write_text('failed once')
            try:
                error=OSError('BitBlt temporarily unavailable')
                error.capture_retryable=True
                error.client_availability={'is_window':True,'visible':True}
                raise error
            except OSError as error:
                raise CaptureError('native capture failed') from error
        return np.full((12,20,3),42,np.uint8)

def test_native_failure_survives_spawn_and_next_capture_restarts(tmp_path):
    capture=CaptureProcess(FailOnce(tmp_path/'failed'),timeout=5,startup_timeout=15,capture_size=(20,12))
    try:
        with pytest.raises(TransientCaptureError) as caught: capture.capture()
        assert caught.value.client_availability['is_window'] is True
        assert capture.process is None and capture.last_observed_at is None
        assert capture._starts==1
        assert capture.capture().size==(20,12)
        assert capture._starts==2 and capture.last_observed_at is not None
    finally:capture.close()

@pytest.mark.parametrize('flag',[0,1,'true',None,{}])
def test_wire_recovery_flag_must_be_boolean(flag):
    capture=CaptureProcess()
    with pytest.raises(CaptureError,match='invalid recovery flag') as caught:
        capture._decode(('error',1,'bad',None,flag),1,time.monotonic()+1)
    assert not isinstance(caught.value,TransientCaptureError)

def test_legacy_errors_and_validation_errors_stay_terminal():
    capture=CaptureProcess()
    for message in [('error',1,'bad size'),('error',1,'bad size',None),('error',1,'bad size',None,False)]:
        with pytest.raises(CaptureError) as caught:capture._decode(message,1,time.monotonic()+1)
        assert not isinstance(caught.value,TransientCaptureError)
    assert not _retryable_capture_error(ValueError('bad config'))
    assert not _retryable_capture_error(CaptureError('recalibrate'))

def test_native_operation_is_retryable_but_size_change_is_not(monkeypatch):
    from test_direct_capture import Native
    from tibia_client import direct_capture
    from tibia_client.window_target import WindowTargetError
    monkeypatch.setattr(direct_capture,'diagnose_client_availability',lambda *_:{'is_window':True})
    monkeypatch.setattr(direct_capture,'_DirectClientBackend',lambda:Native(WindowTargetError('BitBlt failed')))
    with pytest.raises(WindowTargetError) as caught:direct_capture.capture_direct_host_client(41)
    assert _retryable_capture_error(caught.value)
    monkeypatch.setattr(direct_capture,'_DirectClientBackend',lambda:Native())
    with pytest.raises(WindowTargetError) as caught:direct_capture.capture_direct_host_client(41,(81,60))
    assert not _retryable_capture_error(caught.value)

def test_desktop_black_frame_is_transient_and_next_frame_recovers():
    from test_sdk_desktop import FakeHotkey,Token,Provider
    from tibia_client.desktop import DesktopAdapter
    from tibia_client.models import Sensor,SensorKind
    class BlackOnce(Provider):
        def capture(self):
            if self.calls==0:
                self.calls+=1
                return Frame(np.zeros((120,180,3),np.uint8))
            return super().capture()
    adapter=DesktopAdapter(0,hotkey=FakeHotkey(),capture_provider=BlackOnce())
    sensor=Sensor(SensorKind.FRAME_VALID)
    try:
        with pytest.raises(TransientCaptureError):adapter.observe([sensor],Token())
        assert adapter.observe([sensor],Token()).readings[sensor].value is True
    finally:adapter.close()

def test_worker_reported_error_keeps_session_enabled_and_recovers():
    from test_sdk_session import Clock,Adapter,Feature,HEALTH
    from tibia_client import ClientSession
    clock=Clock();adapter=Adapter(clock)
    with pytest.raises(TransientCaptureError) as caught:
        CaptureProcess()._decode(('error',1,'GDI unavailable',None,True),1,time.monotonic()+1)
    with ClientSession(adapter,clock=clock,autostart=False) as session:
        session.start_feature(Feature('readings',(HEALTH,)))
        session.tick_once()
        adapter.observe_error=caught.value
        for _ in range(3):
            clock.advance(.3);session.tick_once()
            assert session.feature_status('readings').active
            assert 'Waiting for capture' in session.feature_status('readings').phase
            assert adapter.closed==0 and not adapter.actions
        adapter.observe_error=None
        clock.advance(.3);session.tick_once()
        assert session.feature_status('readings').active
        assert 'Waiting for capture' not in session.feature_status('readings').phase

def test_coordinates_recover_without_reloading_atlas_and_stop_during_retry():
    from test_coordinate_monitor import Capture,Locator,settings,await_condition
    from tibia_client.coordinate_monitor import CoordinateMonitor
    capture=Capture(TransientCaptureError('temporary'));locator=Locator();loads=[]
    def factory(*_):loads.append(True);return locator
    monitor=CoordinateMonitor(settings(),capture_factory=lambda:capture,locator_factory=factory,interval=.01)
    try:
        monitor.start()
        await_condition(lambda:monitor.state().phase=='Waiting for capture')
        assert monitor.state().sample is None and monitor.state().running
        from test_coordinate_monitor import picture
        capture.frame=picture()
        await_condition(lambda:monitor.state().sample is not None)
        assert len(loads)==1
        capture.frame=TransientCaptureError('temporary again')
        await_condition(lambda:monitor.state().phase=='Waiting for capture')
        monitor.stop();assert monitor.wait()
        assert monitor.state().sample is None and not monitor.state().running
    finally:monitor.close();assert monitor.wait()

def test_readings_recover_after_black_frame_and_clear_old_values():
    from test_resource_monitor import Capture,Reader,VisualReader,ResourceMonitor,picture,await_condition
    first=Capture([picture(),Frame(np.zeros((12,20,3),np.uint8))]);second=Capture()
    calls=[]
    def factory():
        calls.append(True)
        return first if len(calls)==1 else second
    monitor=ResourceMonitor(factory,Reader,visual_reader_factory=VisualReader,interval=.02)
    monitor._cache_path=None
    try:
        monitor.start()
        await_condition(lambda:monitor.state().phase=='Waiting for capture')
        assert monitor.state().preview is None and monitor.state().sample is None
        await_condition(lambda:monitor.state().sample is not None)
        assert len(calls)==2 and first.closed and monitor.state().running
    finally:monitor.close();assert monitor.wait()
