// The desktop profile's Speedometer run (tests/bench/desktop/driver.sh): what the browser renders WebGL with, as one
// console line through serve.py ("SP /log {"webgl": ...}"), before Speedometer starts.
(function () {
    let info = {};
    try {
        const gl = document.createElement("canvas").getContext("webgl");
        if (!gl) info = { webgl: null };
        else {
            const d = gl.getExtension("WEBGL_debug_renderer_info");
            info = { webgl: d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
                     vendor: d ? gl.getParameter(d.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
                     version: gl.getParameter(gl.VERSION) };
        }
    } catch (e) { info = { webgl_error: String(e) }; }
    info.ua = navigator.userAgent;
    try { const x = new XMLHttpRequest(); x.open("POST", "/log", false); x.send(JSON.stringify(info)); } catch (e) {}
})();
