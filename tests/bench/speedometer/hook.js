// The results hook of the Speedometer cases (tests/bench/run_speedometer.py; not part of Speedometer): the guest init
// loads it before Speedometer's resources/main.mjs, so its DOMContentLoaded listener runs first and wraps the
// benchmark client before prepareUI() starts the run (?startAutomatically). It POSTs progress (/log) and the results
// (/done: the score, each iteration's, every suite's mean and values in ms) to serve.py, which prints them.
(function () {
    const t0 = performance.now();
    function post(path, obj) {
        try {
            const x = new XMLHttpRequest();
            x.open("POST", path, false);
            x.send(JSON.stringify(obj));
        } catch (e) {}
    }
    window.addEventListener("error", (e) => post("/log", { error: String(e.message), at: `${e.filename}:${e.lineno}` }));
    window.addEventListener("DOMContentLoaded", () => {
        const c = globalThis.benchmarkClient;
        if (!c) { post("/done", { error: "no benchmarkClient" }); return; }
        post("/log", { loaded: Math.round(performance.now() - t0) });
        const willRunTest = c.willRunTest.bind(c);
        let lastSuite = null;
        c.willRunTest = function (suite, test) {
            if (suite.name !== lastSuite) {
                lastSuite = suite.name;
                post("/log", { suite: suite.name, step: this._finishedTestCount, of: this.stepCount, t: Math.round(performance.now() - t0) });
            }
            return willRunTest(suite, test);
        };
        const finish = c.didFinishLastIteration.bind(c);
        c.didFinishLastIteration = function (metrics) {
            const r = finish(metrics);
            const s = this._computeResults(this._measuredValuesList, "score");
            const suites = {};
            for (const m of Object.values(metrics))
                if (!m.parent && m.children.length > 0) suites[m.name] = { mean: m.mean, values: m.values };
            post("/done", {
                score: s.mean, scoreText: s.formattedMeanAndDelta, perIteration: this._measuredValuesList.map((v) => v.score),
                geomeanMs: metrics.Geomean ? metrics.Geomean.mean : null, suites, ua: navigator.userAgent,
                wallMs: Math.round(performance.now() - t0),
            });
            return r;
        };
        const handleError = c.handleError.bind(c);
        c.handleError = function (e) { post("/done", { error: String(e && e.stack || e) }); return handleError(e); };
    });
})();
