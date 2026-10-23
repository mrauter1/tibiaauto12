import cv2
import numpy as np

from tools import benchmark_fullscreen_ocr as bench


def test_neutral_enlargement_matches_production_pixels():
    rgb = np.array([[[100, 100, 139], [100, 100, 140]],
                    [[255, 255, 255], [99, 100, 100]]], np.uint8)
    expected = cv2.cvtColor(np.array([[0, 255], [0, 255]], np.uint8), cv2.COLOR_GRAY2BGRA)
    expected = np.repeat(np.repeat(expected, 3, axis=0), 3, axis=1)
    np.testing.assert_array_equal(bench.prepare(rgb, 'neutral-mask', 3), expected)
    np.testing.assert_array_equal(bench.prepare(rgb, 'rgb', 1)[..., :3], rgb[..., ::-1])


def test_fit_preserves_complete_frame_and_records_black_bars():
    rgb = np.full((864, 1536, 3), 192, np.uint8)
    result, detail = bench.fit_canvas(rgb)
    assert result.shape == (768, 1024, 3)
    assert detail['content_box'] == [0, 96, 1024, 672]
    assert (result[96:672] == 192).all()
    assert not result[:96].any() and not result[672:].any()


def test_fresh_recognition_calls_and_errors_are_preserved():
    calls = []
    def decoder(bgra, scale):
        calls.append((bgra.shape, scale))
        if len(calls) == 1:
            raise RuntimeError('test failure')
        return [{'text': 'Rat', 'box': [1, 2, 3, 4],
                 'words': [{'text': 'Rat', 'box': [1, 2, 3, 4]}]}]
    result = bench.benchmark([('test', np.zeros((10, 20, 3), np.uint8), {})], 2, decoder)
    assert len(calls) == 12
    assert len(result['invocation_order']) == 12
    first = result['cases'][0]
    assert first['errors'] == 1
    assert first['calls'][0]['error'] == 'RuntimeError: test failure'
    assert first['repeat_latency']['n'] == 2
    assert not first['stable_results']
    assert all(c['repeat_latency']['n'] == 2 for c in result['cases'])
    assert all(c['calls'][1]['words'] == 1 for c in result['cases'])
