# Tibia on the host or inside Windows Sandbox

TibiaAuto12 runs on the host in both modes. Choose **Local Tibia** for a host
game window, or **Windows Sandbox** for a guest game displayed in a host Sandbox
window. Existing profiles without a `window_target` setting use local mode.

Input now uses the `on_error` availability policy. A normal action goes directly
to the selected physical or window-message backend. It does not take a guard
screenshot, compare focus patches, search for dialogs, or check client
availability before each keypress or click. When capture or input reports an
error, the adapter inspects client availability and includes that diagnosis in
the error result. It does not retry an action automatically.

## Local Tibia

Select the game input window and the matching OBS projector capture window,
with the same 1:1 client size, when using the existing OBS integration. Direct
visible-region capture is used when the same game window is selected for capture
and input. Both capture and mouse coordinates use physical pixels.
Local configuration does not need Sandbox viewport or focus samples.

## Windows Sandbox setup

The host sees the Sandbox display window, rather than Tibia's guest HWND.
Sandbox mode captures its visible host-client region, crops a calibrated Tibia
viewport and maps viewport coordinates back to host pixels for physical input.
Sandbox input is **physical only**. It does not use an OBS projector or a guest
helper.

1. Start Sandbox and Tibia. Arrange the guest game at the size and position you
   intend to use. Choose the Sandbox host window in the launcher and open
   **Configure Sandbox area**.
2. Choose **Capture Tibia**. The wizard hides, activates Sandbox once, allows
   350 ms for repainting, and captures the image. It sends no clicks or keys.
3. Drag the full guest Tibia client area in the preview. Exclude its guest title
   bar, outer Sandbox controls, taskbar and unrelated desktop. Preview scaling
   does not scale input: saved rectangles use original physical image pixels,
   with exclusive right and bottom edges.
4. Review the rectangle and choose **Save Sandbox setup**. One capture and the
   viewport rectangle are sufficient. No title-bar patch or inactive sample is
   required. Cancel and closing the wizard discard the draft.
5. Calibrate the character using the cropped game view. If this client's layout
   is not recognized, **Open keyboard modules** runs Timed Spells without
   fabricating image calibration. Readings and Trace Viewer remain available;
   image-dependent automation remains disabled.

The saved target contains `kind`, `host_size` and `viewport`. Older profiles can
still contain `focus_guard` metadata; the error-only input path does not evaluate
it. Explicit legacy probe experiments can still request paired samples.

Save separate profiles for different guest arrangements. A changed host size,
guest game position, guest game size or display scaling requires updating the
viewport and calibration. Moving the outer host window changes screen position
without changing the stored client-relative viewport.

## Runtime behavior

Physical input uses the host mouse and keyboard. Keep Sandbox and the intended
guest game selected while using it. The normal path performs no focus or dialog
preflight. If a native call succeeds while a different application has focus,
that is not necessarily reported as an error by Windows.

Feature start and navigation resume activate the selected host window once.
Subsequent dispatches do not refocus or poll availability. Keys are held for
50 ms and released in a cancellation-safe finally block. Key names are case
insensitive. The actual-module validation record is
[production-module-validation.md](production-module-validation.md).

Visible-region capture reads the pixels currently displayed in the selected
region. A covering window can therefore appear in a capture. The reader still
handles missing or unreadable game observations separately from client
availability; removing the availability guard does not alter feature thresholds
or the meaning of unknown readings.

Capture workers allow up to 15 seconds for cold initialization, separately from
the normal two-second capture deadline. Startup is cancellable. The input path
does not add those capture costs to every keypress or click.

Sandbox isolation settings are unchanged. Clipboard sharing, networking, mapped
folders and ProtectedClient do not expose the guest Tibia HWND to the host. The
implementation uses the visible Sandbox display and host physical input.

The [live probe results](desktop-probe-live-results.md) distinguish actual
accepted operations from older guarded experiments and remaining untested
arrangements. Offline tests cover geometry, error handling, configuration and
cancellation; they do not establish acceptance by a particular game client.
