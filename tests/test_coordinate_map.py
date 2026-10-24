from types import SimpleNamespace
import json
import time
import tkinter as tk

from PIL import Image
import pytest

from modules.CoordinateMap import CoordinateMap, load_floor


@pytest.fixture
def atlas(tmp_path):
    for z, color in [(7, '#00cc00'), (8, '#999999')]:
        Image.new('RGB', (200, 120), color).save(tmp_path/f'floor_{z}.png')
    (tmp_path/'manifest.json').write_text(json.dumps(dict(origin_x=31000,origin_y=32000,
        floors=[dict(floor=z,file=f'floor_{z}.png') for z in (7,8)])))
    return tmp_path


@pytest.fixture(scope='session')
def map_tk_root():
    # Keep one interpreter alive while later tests start capture workers. Each
    # viewer is a Toplevel, as in the application; discarded standalone Tk roots
    # can otherwise be garbage-collected on those workers by Python.
    try:
        root=tk.Tk()
    except tk.TclError:
        pytest.skip('Tk display unavailable')
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def viewer(map_tk_root):
    root=map_tk_root
    host=tk.Toplevel(root)
    host.geometry('500x500')
    widget=CoordinateMap(host)
    widget.pack(fill='both',expand=True)
    root.update()
    yield root,widget
    widget.close()
    host.destroy()


def sample(z=7,reference='Player crosshair'):
    return SimpleNamespace(reference=reference,
        result=SimpleNamespace(accepted=True,x=31080,y=32045,z=z))


def until(root,predicate):
    deadline=time.monotonic()+5
    while not predicate() and time.monotonic()<deadline:
        root.update()
        time.sleep(.01)
    root.update()
    assert predicate()


def test_floor_origin_marker_follow_zoom_pan_and_stale(atlas,viewer):
    root,v=viewer
    v.set_prediction(atlas,sample(),'Predicted')
    until(root,lambda:v._image is not None)
    assert v._origin==(31000,32000)
    assert v.point_on_canvas()==(v.canvas.winfo_width()/2,v.canvas.winfo_height()/2)
    assert v.canvas.find_withtag('position')
    v.fit()
    root.update()
    px,py=v.point_on_canvas()
    assert px==pytest.approx((80.5-100)*v._zoom+v.canvas.winfo_width()/2)
    assert py==pytest.approx((45.5-60)*v._zoom+v.canvas.winfo_height()/2)
    v.zoom(2,(px,py))
    assert v.point_on_canvas()==pytest.approx((px,py))
    v._pan_start(SimpleNamespace(x=10,y=20))
    v._pan(SimpleNamespace(x=30,y=50))
    assert not v.follow.get()
    assert v.point_on_canvas()==pytest.approx((px+20,py+30))
    for status in ('Stale','Unknown','Disabled','Error'):
        v.set_prediction(atlas,sample(),status)
        root.update()
        assert v.point_on_canvas() is None
        assert not v.canvas.find_withtag('position')
        assert v._image is not None
    v.set_prediction(atlas,sample(8,'Map center'),'Map-center estimate')
    until(root,lambda:v._loaded is not None and v._loaded[1]==8)
    assert 'Map-center estimate' in v.caption.get()
    assert v._image.getpixel((0,0))==(153,153,153)


def test_old_floor_load_cannot_replace_new_request(atlas,viewer):
    root,v=viewer
    v.set_prediction(atlas,sample(),'Predicted')
    v.set_prediction(atlas,sample(8),'Predicted')
    until(root,lambda:v._loaded is not None and v._loaded[1]==8)
    assert v._image.getpixel((0,0))==(153,153,153)
    v.close()
    v.close()
    assert v._closed


def test_bad_atlas_reports_error_and_hides_marker(atlas,viewer):
    root,v=viewer
    v.set_prediction(atlas,sample(9),'Predicted')
    until(root,lambda:'Map unavailable' in v.caption.get())
    assert not v.canvas.find_withtag('position')
    assert v._image is None


def test_exact_tie_keeps_marker_visible_and_labels_it(atlas,viewer):
    root,v=viewer
    v.set_prediction(atlas,sample(),'Ambiguous')
    until(root,lambda:v._image is not None)
    assert v.point_on_canvas() is not None
    assert v.canvas.find_withtag('position')
    assert 'Ambiguous' in v.caption.get()
    v.set_prediction(atlas,sample(),'Stale')
    root.update()
    assert not v.canvas.find_withtag('position')


def test_loader_rejects_missing_floor_and_path_escape(atlas):
    with pytest.raises(ValueError,match='absent'):
        load_floor(atlas,9)
    manifest=json.loads((atlas/'manifest.json').read_text())
    manifest['floors'][0]['file']='../outside.png'
    (atlas/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='inside'):
        load_floor(atlas,7)


def test_manual_floor_preserves_view_and_auto_restores_prediction(atlas,viewer):
    root,v=viewer
    v.set_prediction(atlas,sample(),'Predicted')
    until(root,lambda:v._loaded is not None)
    v.zoom(2.)
    v._pan_start(SimpleNamespace(x=10,y=10))
    v._pan(SimpleNamespace(x=30,y=35))
    center,zoom=v._center,v._zoom
    v.floor.set('8')
    v._select_floor()
    until(root,lambda:v._loaded is not None and v._loaded[1]==8)
    assert v._center==center and v._zoom==zoom
    assert v.point_on_canvas() is None
    assert not v.canvas.find_withtag('position')
    assert 'prediction is on floor 7' in v.caption.get()
    v.set_prediction(atlas,sample(),'Predicted')
    root.update()
    assert v._loaded[1]==8  # New predictions must not override manual selection.
    v.floor.set('Auto')
    v._select_floor()
    until(root,lambda:v._loaded is not None and v._loaded[1]==7)
    assert v.canvas.find_withtag('position')
    assert v._center==center and v._zoom==zoom


def test_floor_browsing_without_prediction_and_height_buttons(atlas,viewer):
    root,v=viewer
    v.set_prediction(atlas,None,'Disabled')
    assert v._future is None
    v.floor.set('8')
    v._select_floor()
    until(root,lambda:v._loaded is not None and v._loaded[1]==8)
    assert 'no current position' in v.caption.get()
    assert not v.canvas.find_withtag('position')
    v._step_floor(-1)
    until(root,lambda:v._loaded is not None and v._loaded[1]==7)
    assert v.floor.get()=='7'
    v._step_floor(1)
    until(root,lambda:v._loaded is not None and v._loaded[1]==8)
    v.floor.set('0');v._step_floor(-1)
    assert v.floor.get()=='0'
    v.floor.set('15');v._step_floor(1)
    assert v.floor.get()=='15'


def test_manual_selection_wins_over_pending_auto_floor_load(atlas,viewer):
    root,v=viewer
    v.set_prediction(atlas,sample(),'Predicted')
    v.floor.set('8')
    v._select_floor()
    until(root,lambda:v._loaded is not None and v._loaded[1]==8)
    assert v.point_on_canvas() is None


def test_coordinates_window_has_room_for_map_and_stops_viewer(map_tk_root):
    from modules.CoordinateMonitor import CoordinateMonitorWindow
    root=map_tk_root
    ui=CoordinateMonitorWindow(root,box=(0,0,80,80))
    try:
        root.update()
        assert ui.world_map.canvas.winfo_width()>=250
        assert ui.world_map.canvas.winfo_height()>=300
        assert not ui.enabled.get()
        assert ui.world_map._future is None
        assert ui.world_map._folder is not None
        assert ui._settings().floor is None
        ui.world_map.floor.set('8')
        assert ui._settings().floor is None  # Viewer selection is independent.
    finally:
        ui.close()
    assert ui.world_map._closed
