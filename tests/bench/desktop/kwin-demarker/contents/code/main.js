// natemu's desktop profile (tests/bench/run_desktop.py): KWin prints each window's arrival, activation and removal as
// "DEWIN EVENT CLASS pid=PID ms=WALLCLOCK_MS", which the harness's driver reads from the journal (a Wayland window
// arrives when its surface is first mapped), and registers the shortcuts the driver invokes over D-Bus to move, resize
// and switch windows one step at a time ("DEWIN step NAME ...").
function report(ev, w) {
    if (!w) return;
    print("DEWIN " + ev + " " + (w.resourceClass || "?") + " pid=" + w.pid + " normal=" + (w.normalWindow ? 1 : 0) +
          " ms=" + Date.now());
}
workspace.windowAdded.connect(function (w) {
    report("add", w);
    w.readyForPaintingChanged.connect(function () { report("ready", w); });
});
workspace.windowRemoved.connect(function (w) { report("remove", w); });
workspace.windowActivated.connect(function (w) { report("active", w); });

function step(name, f) {
    registerShortcut("DE " + name, "DE " + name, "", function () {
        var w = workspace.activeWindow;
        if (!w || !w.normalWindow) { print("DEWIN step " + name + " none ms=" + Date.now()); return; }
        var g = w.frameGeometry;
        var n = f(g);
        if (n) w.frameGeometry = n;
        print("DEWIN step " + name + " " + w.resourceClass + " ms=" + Date.now());
    });
}
step("Move", function (g) { return { x: g.x + 24, y: g.y + 12, width: g.width, height: g.height }; });
step("Grow", function (g) { return { x: g.x, y: g.y, width: g.width + 24, height: g.height + 16 }; });
step("Shrink", function (g) { return { x: g.x, y: g.y, width: g.width - 24, height: g.height - 16 }; });
step("Place", function (g) { return { x: 80, y: 60, width: 900, height: 600 }; });
registerShortcut("DE Next Window", "DE Next Window", "", function () {
    var all = workspace.stackingOrder.filter(function (w) { return w.normalWindow; });
    if (all.length < 2) { print("DEWIN step NextWindow none ms=" + Date.now()); return; }
    workspace.activeWindow = all[0];             // the bottom-most normal window comes to the front
    print("DEWIN step NextWindow " + all[0].resourceClass + " ms=" + Date.now());
});
registerShortcut("DE Close Top", "DE Close Top", "", function () {
    var all = workspace.stackingOrder.filter(function (w) { return w.normalWindow; });
    if (!all.length) { print("DEWIN step CloseTop none ms=" + Date.now()); return; }
    var w = all[all.length - 1];
    print("DEWIN step CloseTop " + w.resourceClass + " pid=" + w.pid + " ms=" + Date.now());
    w.closeWindow();
});
print("DEWIN loaded demarker ms=" + Date.now());
