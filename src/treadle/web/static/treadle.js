// Treadle dashboard script. Every page is complete without it: it only confirms marked
// forms and keeps an active run's page up to date without reloading by hand.
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var message = event.target.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  var run = document.querySelector("[data-run-active='true']");
  if (!run) {
    return;
  }
  var base = "/api/v1/runs/" + encodeURIComponent(run.getAttribute("data-run-id"));
  var after = Number(run.getAttribute("data-after")) || 0;
  var log = document.getElementById("live-log");
  var status = document.getElementById("run-status");
  var pause = document.getElementById("pause-updates");
  var shown = null;
  var paused = false;
  var busy = false;
  var timer = null;

  pause.hidden = false;
  pause.addEventListener("click", function () {
    paused = !paused;
    pause.textContent = paused ? "Resume updates" : "Pause updates";
    pause.setAttribute("aria-pressed", String(paused));
  });

  function getJSON(url) {
    return fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("the server answered " + response.status);
        }
        return response.json();
      });
  }

  function appendLines(lines) {
    lines.forEach(function (line) {
      var where = line.step ? line.step + "#" + line.attempt : "run";
      // append() adds text, never markup, so log output can't inject HTML.
      log.append(line.ts.slice(11, 19) + "Z " + where + " " + line.stream + " | " + line.text + "\n");
    });
  }

  function stop(message) {
    window.clearInterval(timer);
    status.textContent = message;
  }

  function poll() {
    if (paused || busy) {
      return;
    }
    busy = true;
    Promise.all([getJSON(base), getJSON(base + "/logs?after=" + after)])
      .then(function (results) {
        var current = results[0];
        var page = results[1];
        appendLines(page.lines);
        after = page.next_after;
        if (current.status !== shown && shown !== null) {
          status.textContent = "Status: " + current.status.replace("_", " ");
        }
        shown = current.status;
        if (current.status !== "queued" && current.status !== "running" && page.complete) {
          stop("The run finished. Reloading the page.");
          window.location.reload();
        }
      })
      .catch(function (error) {
        stop("Live updates stopped (" + error.message + "). Use Refresh to reload the page.");
      })
      .finally(function () {
        busy = false;
      });
  }

  timer = window.setInterval(poll, 2000);
})();
