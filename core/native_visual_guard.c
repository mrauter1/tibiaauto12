/* Optional exact duplicate-panel candidate filter. Built only by the explicit
 * tools/build_visual_native.py command, which generates this identity header.
 * Python retains all full label/geometry checks and the >128 rejection rule.
 */
#include <stdint.h>
#include <stddef.h>
#include "native_visual_build.h"

#if defined(_WIN32)
#define GUARD_API __declspec(dllexport)
#else
#define GUARD_API
#endif

GUARD_API int visual_guard_abi(void) { return 1; }
GUARD_API const char *visual_guard_source_id(void) { return VISUAL_GUARD_SOURCE_ID; }

GUARD_API int visual_guard_candidates(
        const uint8_t *rgb, int height, int width,
        const int *dy, const int *dx, const uint8_t *ink,
        const uint8_t *coarse, int count, int out_width, int out_height,
        int *output, int capacity) {
    int found = 0;
    if (!rgb || !dy || !dx || !ink || !coarse || !output ||
        width <= 0 || height <= 0 || count <= 0 || count > 20 || capacity < 129 ||
        out_width <= 0 || out_height <= 0 || out_width > width || out_height > height)
        return -1;
    for (int i = 0; i < count; ++i) {
        /* Subtraction avoids overflow from adversarial offset additions. */
        if (dx[i] < 0 || dy[i] < 0 || dx[i] > width - out_width ||
            dy[i] > height - out_height || ink[i] > 1 || coarse[i] > 1) return -1;
    }
    for (int y = 0; y < out_height; ++y) {
        for (int x = 0; x < out_width; ++x) {
            int matched = 1;
            for (int i = 0; i < count && i < 4; ++i) {
                const uint8_t *p = rgb + ((size_t)(y + dy[i]) * width + x + dx[i]) * 3;
                int bright = p[0] >= 130 && p[1] >= 130 && p[2] >= 130;
                if (bright != coarse[i]) { matched = 0; break; }
            }
            if (!matched) continue;
            for (int i = 0; i < count; ++i) {
                const uint8_t *p = rgb + ((size_t)(y + dy[i]) * width + x + dx[i]) * 3;
                int low = p[0] < p[1] ? p[0] : p[1];
                low = low < p[2] ? low : p[2];
                int high = p[0] > p[1] ? p[0] : p[1];
                high = high > p[2] ? high : p[2];
                int foreground = low >= 130 && high - low < 35;
                if (foreground != ink[i]) { matched = 0; break; }
            }
            if (matched) {
                output[found * 2] = x;
                output[found * 2 + 1] = y;
                if (++found == 129) return found;
            }
        }
    }
    return found;
}
