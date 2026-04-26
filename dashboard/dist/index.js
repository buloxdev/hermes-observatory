/**
 * Hermes Observatory dashboard plugin.
 *
 * Plain IIFE bundle that uses the Hermes Dashboard Plugin SDK globals.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const PLUGINS = window.__HERMES_PLUGINS__;
  if (!SDK || !PLUGINS) return;

  const { React } = SDK;
  const { useEffect, useMemo, useState } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;

  const API = "/api/plugins/hermes-observatory/snapshot";

  function h(type, props) {
    const children = Array.prototype.slice.call(arguments, 2);
    return React.createElement(type, props, ...children);
  }

  function fmtNumber(value) {
    return Number(value || 0).toLocaleString();
  }

  function last(values) {
    return values && values.length ? values[values.length - 1] : 0;
  }

  function spark(values) {
    if (!values || !values.length) return "no data";
    const blocks = "▁▂▃▄▅▆▇█";
    const slice = values.slice(-36);
    const min = Math.min.apply(null, slice);
    const max = Math.max.apply(null, slice);
    if (min === max) return "▁".repeat(slice.length);
    return slice.map(function (v) {
      const n = (v - min) / (max - min);
      return blocks[Math.max(0, Math.min(blocks.length - 1, Math.floor(n * (blocks.length - 1))))];
    }).join("");
  }

  function tone(priority) {
    if (priority === "P0") return "destructive";
    if (priority === "P1") return "secondary";
    return "outline";
  }

  function Metric(props) {
    return h(Card, { className: "observatory-card observatory-metric" },
      h(CardContent, { className: "pt-5" },
        h("div", { className: "observatory-label" }, props.label),
        h("div", { className: "observatory-value", style: { color: props.color || "#e2e8f0" } }, props.value),
        h("div", { className: "observatory-detail" }, props.detail),
      ),
    );
  }

  function ListCard(props) {
    return h(Card, { className: "observatory-card" },
      h(CardHeader, null,
        h(CardTitle, { className: "text-base" }, props.title),
      ),
      h(CardContent, null,
        h("div", { className: "observatory-list" }, props.children),
      ),
    );
  }

  function Row(props) {
    return h("div", { className: "observatory-row" },
      h("div", { className: "observatory-row-main" },
        h("div", { className: "observatory-row-title" }, props.title),
        h("div", { className: "observatory-row-sub" }, props.sub || ""),
      ),
      props.badge ? h(Badge, { variant: props.variant || "outline" }, props.badge) : null,
    );
  }

  function EmptyRow(props) {
    return h("div", { className: "observatory-row" },
      h("div", { className: "observatory-row-main" },
        h("div", { className: "observatory-row-title observatory-muted" }, props.children),
      ),
    );
  }

  function ObservatoryPage() {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    function load() {
      setLoading(true);
      SDK.fetchJSON(API)
        .then(function (snapshot) {
          setData(snapshot);
          setError(null);
        })
        .catch(function (err) {
          setError(String(err.message || err));
        })
        .finally(function () {
          setLoading(false);
        });
    }

    useEffect(function () {
      load();
      const timer = setInterval(load, 5000);
      return function () { clearInterval(timer); };
    }, []);

    const metrics = useMemo(function () {
      if (!data) return null;
      const reliability = data.reliability.avg == null ? "N/A" : data.reliability.avg + "/100";
      return {
        gateway: data.gateway.running ? "ONLINE" : "OFFLINE",
        gatewayColor: data.gateway.running ? "#4ade80" : "#f87171",
        reliability,
        tokens: fmtNumber(last(data.trace.tokens)),
        tools: fmtNumber(last(data.trace.tools)),
      };
    }, [data]);

    if (!data && loading) {
      return h("div", { className: "observatory-root" },
        h(Card, { className: "observatory-card" },
          h(CardContent, { className: "pt-6 observatory-muted" }, "Loading Observatory telemetry..."),
        ),
      );
    }

    if (error && !data) {
      return h("div", { className: "observatory-root" },
        h(Card, { className: "observatory-card" },
          h(CardContent, { className: "pt-6" },
            h("div", { className: "text-red-300" }, "Observatory API is unavailable."),
            h("div", { className: "observatory-muted text-sm mt-2" }, error),
          ),
        ),
      );
    }

    return h("div", { className: "observatory-root" },
      h("section", { className: "observatory-hero" },
        h("div", { className: "observatory-title" },
          h("div", null,
            h("h1", null, "Hermes Observatory"),
            h("div", { className: "observatory-subtitle" }, "Live operational intelligence for agents, gateways, profiles, and automation."),
          ),
          h("div", { className: "flex items-center gap-2" },
            h(Badge, { variant: "outline" }, "updated " + (data.updated_at || "-")),
            h(Button, { onClick: load, disabled: loading }, loading ? "Refreshing" : "Refresh"),
          ),
        ),
      ),

      h("div", { className: "observatory-grid" },
        h(Metric, { label: "Gateway", value: metrics.gateway, detail: "pid " + (data.gateway.pid || "-"), color: metrics.gatewayColor }),
        h(Metric, { label: "Reliability", value: metrics.reliability, detail: fmtNumber(data.reliability.count) + " scored sessions", color: "#fb7185" }),
        h(Metric, { label: "Token Burn", value: metrics.tokens, detail: "latest session window", color: "#2dd4bf" }),
        h(Metric, { label: "Tool Calls", value: metrics.tools, detail: "latest session window", color: "#f59e0b" }),
      ),

      h("div", { className: "observatory-grid-2" },
        h(ListCard, { title: "Active Profiles" },
          data.active_profiles.length
            ? data.active_profiles.map(function (p) {
                return h(Row, {
                  key: p.name,
                  title: p.name,
                  sub: "platforms: " + (p.platforms.join(", ") || "none") + " · seen " + p.updated,
                  badge: p.state.toUpperCase(),
                  variant: p.state === "online" ? "outline" : "secondary",
                });
              })
            : h(EmptyRow, null, "No active gateway profiles"),
        ),
        h(ListCard, { title: "Next Moves" },
          data.next_moves.map(function (move, index) {
            return h(Row, {
              key: index,
              title: move.title,
              sub: move.why,
              badge: move.priority,
              variant: tone(move.priority),
            });
          }),
        ),
      ),

      h("div", { className: "observatory-grid-2" },
        h(ListCard, { title: "Anomaly Radar" },
          data.reliability.issues.length
            ? data.reliability.issues.map(function (issue) {
                return h(Row, {
                  key: issue.id,
                  title: issue.id,
                  sub: issue.summary || "No details",
                  badge: String(issue.score),
                  variant: issue.score < 50 ? "secondary" : "outline",
                });
              })
            : h(EmptyRow, null, "No reliability issues found"),
        ),
        h(Card, { className: "observatory-card" },
          h(CardHeader, null, h(CardTitle, { className: "text-base" }, "Signal Trace")),
          h(CardContent, null,
            h("div", { className: "observatory-label" }, "tokens"),
            h("div", { className: "observatory-spark" }, spark(data.trace.tokens)),
            h("div", { className: "observatory-label mt-4" }, "tools"),
            h("div", { className: "observatory-spark", style: { color: "#fb7185" } }, spark(data.trace.tools)),
          ),
        ),
      ),

      h(ListCard, { title: "Recent Sessions" },
        data.sessions.slice(0, 8).map(function (session) {
          return h(Row, {
            key: session.id,
            title: session.title === "-" ? session.id : session.title,
            sub: session.source + "/" + session.user + " · " + session.model + " · " + fmtNumber(session.tokens) + " tokens · " + fmtNumber(session.tools) + " tools",
            badge: session.seen,
          });
        }),
      ),
    );
  }

  PLUGINS.register("hermes-observatory", ObservatoryPage);
})();
