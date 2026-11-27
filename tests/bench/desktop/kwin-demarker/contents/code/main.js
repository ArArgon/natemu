// natemu's desktop profile (tests/bench/run_desktop.py): KWin prints each window's arrival, activation and removal as
// "DEWIN EVENT CLASS pid=PID ms=WALLCLOCK_MS", which the harness's driver reads from the journal (a Wayland window
// arrives when its surface is first mapped), and registers the shortcuts the driver invokes over D-Bus to move, resize
// and switch windows one step at a time ("DEWIN step NAME ...").
function clean(s) {
    return s === undefined || s === null ? "none" : String(s).replace(/\s+/g, "_");
}
function report(ev, w) {
    if (!w) return;
    print("DEWIN " + ev + " " + (w.resourceClass || "?") + " pid=" + w.pid + " normal=" + (w.normalWindow ? 1 : 0) +
          " id=" + clean(w.internalId) + " ms=" + Date.now());
}
function desktopInfo() {
    var d = workspace.currentDesktop;
    if (!d) return "desk=none deskid=none";
    var num = d.x11DesktopNumber !== undefined ? d.x11DesktopNumber : "none";
    return "desk=" + num + " deskid=" + clean(d.id);
}
function winState(w) {
    if (!w) return "win=none cls=none pid=0 normal=0 geom=none max=none fs=0";
    var g = w.frameGeometry;
    var geom = g ? (Math.round(g.x) + "," + Math.round(g.y) + "," + Math.round(g.width) + "," + Math.round(g.height)) : "none";
    var max = w.maximizeMode !== undefined ? w.maximizeMode : "none";
    var fs = w.fullScreen ? 1 : 0;
    return "win=" + clean(w.internalId) + " cls=" + clean(w.resourceClass || "?") + " pid=" + w.pid +
           " normal=" + (w.normalWindow ? 1 : 0) + " geom=" + geom + " max=" + max + " fs=" + fs;
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
registerShortcut("DE State", "DE State", "", function () {
    var ov = typeof workspace.isEffectActive === "function" ? (workspace.isEffectActive("overview") ? 1 : 0) : "none";
    print("DEWIN state " + desktopInfo() + " ov=" + ov + " " + winState(workspace.activeWindow) + " ms=" + Date.now());
});
registerShortcut("DE Next Window", "DE Next Window", "", function () {
    var prev = workspace.activeWindow;
    var all = workspace.stackingOrder.filter(function (w) { return w.normalWindow; });
    if (all.length < 2) { print("DEWIN step NextWindow none ms=" + Date.now()); return; }
    var next = all[0];
    workspace.activeWindow = next;             // the bottom-most normal window comes to the front
    var cur = workspace.activeWindow;
    print("DEWIN step NextWindow " + clean(next.resourceClass) + " from=" + clean(prev && prev.internalId) +
          " to=" + clean(next.internalId) + " now=" + clean(cur && cur.internalId) + " ms=" + Date.now());
});
registerShortcut("DE Close Top", "DE Close Top", "", function () {
    var all = workspace.stackingOrder.filter(function (w) { return w.normalWindow; });
    if (!all.length) { print("DEWIN step CloseTop none ms=" + Date.now()); return; }
    var w = all[all.length - 1];
    print("DEWIN step CloseTop " + w.resourceClass + " pid=" + w.pid + " ms=" + Date.now());
    w.closeWindow();
});
print("DEWIN loaded demarker ms=" + Date.now());
