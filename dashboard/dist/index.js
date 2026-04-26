/**
 * Observatory Dashboard Plugin
 * Real-time agent health monitoring
 */

(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { Card, CardHeader, CardTitle, CardContent, Badge } = SDK.components;
  const { useState, useEffect } = SDK.hooks;
  const { cn } = SDK.utils;

  function formatTime(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleString(undefined, { hour12: false });
  }

  function ObservatoryPage() {
    const [gateway, setGateway] = React.useState(null);
    const [profiles, setProfiles] = React.useState([]);
    const [scores, setScores] = React.useState({ sessions: [] });
    const [loading, setLoading] = React.useState(true);

    useEffect(() => {
      function refresh() {
        setLoading(true);
        Promise.all([
          SDK.fetchJSON("/api/plugins/hermes-observatory/gateway"),
          SDK.fetchJSON("/api/plugins/hermes-observatory/scores")
        ]).then(([gw, sc]) => {
          setGateway(gw);
          setProfiles(gw.profiles || []);
          setScores(sc);
        }).catch(err => {
          console.error("Observatory fetch error:", err);
        }).finally(() => setLoading(false));
      }
      refresh();
      // 10s auto-refresh
      const interval = setInterval(refresh, 10000);
      return () => clearInterval(interval);
    }, []);

    if (loading && !gateway) {
      return React.createElement("div", { className: "flex items-center justify-center p-8" },
        React.createElement("div", { className: "text-sm text-muted-foreground" }, "Loading Observatory…")
      );
    }

    const gwState = gateway?.gateway?.state || "unknown";
    const platforms = Object.entries(gateway?.gateway?.platforms || {});
    const connectedPlats = platforms.filter(([,p]) => p.state === "connected").length;
    const profileCount = profiles.length;
    const onlineProfiles = profiles.filter(p => p.status === "online").length;
    const alertCount = scores.sessions?.length || 0;
    const hasCrit = scores.sessions?.some(s => s.health === "critical");

    // Status color helpers
    const stateColor = gwState === "running" ? "text-green" : gwState === "offline" ? "text-pink" : "text-orange";
    const badgeVar = gwState === "running" ? "green" : gwState === "offline" ? "destructive" : "secondary";

    return React.createElement("div", { className: "flex flex-col gap-6" },

      // ── Header ──
      React.createElement(Card, null,
        React.createElement(CardHeader, null,
          React.createElement("div", { className: "flex items-center gap-3" },
            React.createElement(CardTitle, { className: "text-lg" }, "🌌 Observatory"),
            React.createElement(Badge, { variant: badgeVar }, gwState)
          ),
          React.createElement("div", { className: "text-xs text-muted-foreground mt-1" },
            "Real-time agent health — gateway, profiles, and reliability scores")
        )
      ),

      // ── Stats row ──
      React.createElement("div", { className: "grid grid-cols-4 gap-4" },

        // Gateway
        React.createElement(Card, null,
          React.createElement(CardHeader, null,
            React.createElement(CardTitle, { className: "text-sm font-medium text-muted-foreground" }, "Gateway")
          ),
          React.createElement(CardContent, null,
            React.createElement("div", { className: cn("text-2xl font-mono font-bold", stateColor) }, gwState),
            React.createElement("div", { className: "text-xs text-muted-foreground mt-1" },
              `${connectedPlats}/${platforms.length} platforms`)
          )
        ),

        // Profiles
        React.createElement(Card, null,
          React.createElement(CardHeader, null,
            React.createElement(CardTitle, { className: "text-sm font-medium text-muted-foreground" }, "Profiles")
          ),
          React.createElement(CardContent, null,
            React.createElement("div", { className: "text-2xl font-mono font-bold text-orange" }, profileCount),
            React.createElement("div", { className: "text-xs text-muted-foreground mt-1" },
              `${onlineProfiles} online, ${profileCount - onlineProfiles} offline`)
          )
        ),

        // Cron jobs
        React.createElement(Card, null,
          React.createElement(CardHeader, null,
            React.createElement(CardTitle, { className: "text-sm font-medium text-muted-foreground" }, "Cron Jobs")
          ),
          React.createElement(CardContent, null,
            React.createElement("div", { className: "text-2xl font-mono font-bold text-cyan" }, "—"),
            React.createElement("div", { className: "text-xs text-muted-foreground mt-1" }, "loading…")
          )
        ),

        // Alerts
        React.createElement(Card, null,
          React.createElement(CardHeader, null,
            React.createElement(CardTitle, { className: "text-sm font-medium text-muted-foreground" }, "Health Alerts")
          ),
          React.createElement(CardContent, null,
            React.createElement("div", { className: cn("text-2xl font-mono font-bold", alertCount > 0 ? "text-pink" : "text-green") }, alertCount),
            React.createElement("div", { className: "text-xs text-muted-foreground mt-1" },
              alertCount > 0 ? "sessions need attention" : "all healthy")
          )
        )
      ),

      // ── Two-column grid ──
      React.createElement("div", { className: "grid grid-cols-2 gap-4" },

        // ── Profiles list ──
        React.createElement(Card, null,
          React.createElement(CardHeader, null,
            React.createElement(CardTitle, { className: "text-sm" }, "Profiles")
          ),
          React.createElement(CardContent, null,
            profiles.length === 0
              ? React.createElement("p", { className: "text-sm text-muted-foreground" }, "No profiles found")
              : React.createElement("div", { className: "space-y-2" },
                  profiles.map(p => React.createElement("div", {
                      key: p.name,
                      className: "flex items-center justify-between py-2 border-b border-border last:border-0"
                    },
                    React.createElement("div", { className: "flex items-center gap-2" },
                      React.createElement("span", {
                        className: cn("w-2 h-2 rounded-full", p.status === "online" ? "bg-green" : "bg-orange")
                      }),
                      React.createElement("span", { className: "text-sm font-mono" }, p.name)
                    ),
                    React.createElement(Badge, { variant: p.status === "online" ? "outline" : "secondary", className: "text-xs" },
                      p.status)
                  ))
                )
          )
        ),

        // ── Health alerts ──
        React.createElement(Card, null,
          React.createElement(CardHeader, null,
            React.createElement(CardTitle, { className: "text-sm flex items-center gap-2" },
              "Low-Score Sessions",
              alertCount > 0 && React.createElement(Badge, { variant: "destructive" }, `${alertCount}`)
            )
          ),
          React.createElement(CardContent, null,
            !scores.sessions || scores.sessions.length === 0
              ? React.createElement("div", { className: "flex items-center gap-2 py-4 text-green text-sm" },
                  "✓ All sessions healthy"
                )
              : React.createElement("div", { className: "space-y-3" },
                  scores.sessions.map(s => React.createElement("div", {
                      key: s.session_id,
                      className: "p-3 rounded-lg border bg-card/50"
                    },
                    React.createElement("div", { className: "flex items-start justify-between gap-2" },
                      React.createElement("div", { className: "flex-1 min-w-0" },
                        React.createElement("div", { className: "flex items-center gap-2 mb-1" },
                          React.createElement("span", {
                            className: cn("w-2 h-2 rounded-full", s.health === "critical" ? "bg-pink" : "bg-orange")
                          }),
                          React.createElement("span", { className: "text-sm font-mono truncate" }, s.session_id),
                          React.createElement(Badge, {
                            variant: s.health === "critical" ? "destructive" : "secondary",
                            className: "text-[10px]"
                          }, s.health)
                        ),
                        React.createElement("div", { className: "text-xs text-muted-foreground grid grid-cols-4 gap-x-2 gap-y-1 mt-2" },
                          React.createElement("span", null, `Score: ${s.score}`),
                          React.createElement("span", null, `C:${s.consistency}`),
                          React.createElement("span", null, `G:${s.grounding}`),
                          React.createElement("span", null, `T:${s.tool_accuracy}`)
                        )
                      )
                    )
                  ))
                )
          )
        )
      ),

      // ── Gateway details (full-width) ──
      React.createElement(Card, null,
        React.createElement(CardHeader, null,
          React.createElement(CardTitle, { className: "text-sm" }, "Gateway Details")
        ),
        React.createElement(CardContent, null,
          React.createElement("div", { className: "grid grid-cols-3 gap-8" },
            // State
            React.createElement("div", null,
              React.createElement("div", { className: "text-xs text-muted-foreground uppercase tracking-wide mb-1" }, "Overall State"),
              React.createElement("div", { className: cn("text-xl font-bold font-mono", stateColor) }, gwState)
            ),
            // Platforms
            React.createElement("div", null,
              React.createElement("div", { className: "text-xs text-muted-foreground uppercase tracking-wide mb-2" }, "Platforms"),
              platforms.length === 0
                ? React.createElement("div", { className: "text-sm text-muted-foreground" }, "None configured")
                : React.createElement("div", { className: "space-y-1" },
                    platforms.map(([name, p]) => React.createElement("div", {
                        key: name,
                        className: "flex items-center justify-between text-sm py-1 border-b border-border/50 last:border-0"
                      },
                      React.createElement("span", { className: "font-mono" }, name),
                      React.createElement(Badge, {
                        variant: p.state === "connected" ? "outline" : "secondary"
                      }, p.state)
                    ))
                  )
            ),
            // Synced at
            React.createElement("div", null,
              React.createElement("div", { className: "text-xs text-muted-foreground uppercase tracking-wide mb-1" }, "Last Updated"),
              React.createElement("div", { className: "text-sm font-mono text-muted-foreground" },
                gateway?.updated_at ? formatTime(gateway.updated_at) : "—")
            )
          )
        )
      )
    );
  }

  // Register with Hermes plugin registry
  window.__HERMES_PLUGINS__.register("hermes-observatory", ObservatoryPage);
})();
