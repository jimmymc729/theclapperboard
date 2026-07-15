// Fires a GA4 custom event if the Google tag loaded (it can fail silently
// under ad blockers or with analytics consent declined, which is fine —
// every call site here is best-effort engagement data, never anything the
// site depends on functioning). GA already auto-attaches page_location to
// every event, so callers don't need to pass which page this happened on.
function trackEvent(name, params) {
  if (typeof gtag === "function") gtag("event", name, params || {});
}

// Rewrites an existing share_row() (see build_site.py) in place once a
// personalized score is known — e.g. "averaged 2.3 years off" — rather
// than building the row from scratch. build_site.py already bakes in a
// working share row with generic fallback copy (so sharing still works
// even if this never runs), including the real canonical page URL on the
// Copy Link button's data-url; this just reads that URL back out and
// rebuilds the other five links' hrefs around the new text, mirroring
// the exact same URL formats as the Python share_row() function.
function refreshShareText(rowId, text) {
  var row = document.getElementById(rowId);
  if (!row) return;
  var copyBtn = row.querySelector('[data-method="copy"]');
  var urlRaw = copyBtn && copyBtn.getAttribute("data-url");
  if (!urlRaw) return;

  var url = encodeURIComponent(urlRaw);
  var textEnc = encodeURIComponent(text);
  var hrefs = {
    twitter: "https://twitter.com/intent/tweet?text=" + textEnc + "&url=" + url,
    // Bluesky's compose intent takes one combined "text" field, no
    // separate url param — same quirk as the Python version.
    bluesky: "https://bsky.app/intent/compose?text=" + encodeURIComponent(text + " " + urlRaw),
    facebook: "https://www.facebook.com/sharer/sharer.php?u=" + url,
    reddit: "https://www.reddit.com/submit?url=" + url + "&title=" + textEnc,
    whatsapp: "https://api.whatsapp.com/send?text=" + textEnc + "%20" + url,
    email: "mailto:?subject=" + textEnc + "&body=" + textEnc + "%20" + url,
  };

  Object.keys(hrefs).forEach(function (method) {
    var el = row.querySelector('[data-method="' + method + '"]');
    if (el) el.setAttribute("href", hrefs[method]);
  });
}

// Reaction buttons on post pages. This is intentionally a purely local,
// per-visitor counter (stored in this browser's localStorage) — it does NOT
// simulate or fake shared/global engagement numbers. Each button just
// tracks whether *you* clicked it, so refreshing doesn't lose your reaction
// but it's not pretending to show what anyone else thinks.
(function () {
  var buttons = document.querySelectorAll(".reaction-btn");
  if (!buttons.length) return;

  buttons.forEach(function (btn) {
    var slug = btn.getAttribute("data-slug");
    var reaction = btn.getAttribute("data-reaction");
    var key = "clapperboard-reaction:" + slug + ":" + reaction;
    var countEl = btn.querySelector(".reaction-count");

    var stored = parseInt(localStorage.getItem(key) || "0", 10);
    if (stored > 0) {
      countEl.textContent = stored;
      btn.classList.add("reacted");
    }

    btn.addEventListener("click", function () {
      var current = parseInt(localStorage.getItem(key) || "0", 10);
      var next = btn.classList.contains("reacted") ? 0 : current + 1;
      localStorage.setItem(key, next);
      countEl.textContent = next;
      btn.classList.toggle("reacted", next > 0);
      trackEvent("reaction_click", { reaction: reaction, active: next > 0 });
    });
  });
})();

// Share buttons — regular per-post share rows, the ones baked into each
// quiz result card, and the ones inside each Games reveal. A single
// delegated listener covers all of them, distinguishing context (and
// pulling out which quiz result was being shown, if any) by walking up the
// DOM from whatever was clicked. The selector covers both <a> links (X,
// Facebook, Reddit, WhatsApp, email) and the Copy Link <button>.
(function () {
  var shareEls = document.querySelectorAll(".share-row a[data-method], .share-row button[data-method]");
  if (!shareEls.length) return;

  shareEls.forEach(function (el) {
    el.addEventListener("click", function () {
      var quizResult = el.closest(".quiz-result");
      trackEvent("share_click", {
        method: el.getAttribute("data-method"),
        context: quizResult ? "quiz_result" : "post",
        result: quizResult ? quizResult.getAttribute("data-result") : undefined,
      });

      // Copy Link is the one share "method" that isn't just a plain link
      // navigation — it needs to actually write to the clipboard and give
      // the person some visible confirmation it worked.
      if (el.getAttribute("data-method") === "copy") {
        var url = el.getAttribute("data-url");
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(url).then(function () {
            var original = el.textContent;
            el.textContent = "✓";
            window.setTimeout(function () {
              el.textContent = original;
            }, 1500);
          });
        }
      }
    });
  });
})();

// Newest/Trending toggle. build_site.py renders both orderings of a post
// list fully into the page at build time (see view_toggle() there); this
// just shows one and hides the other. Generic across however many toggle
// groups end up on a page (homepage, All Posts, each category page — never
// more than one per page today, but nothing here assumes that).
(function () {
  var groups = document.querySelectorAll(".view-toggle-group");
  if (!groups.length) return;

  groups.forEach(function (group) {
    var buttons = group.querySelectorAll(".view-toggle-btn");
    var panels = group.querySelectorAll("[data-view-panel]");

    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var target = btn.getAttribute("data-view");
        buttons.forEach(function (b) { b.classList.toggle("active", b === btn); });
        panels.forEach(function (p) {
          p.hidden = p.getAttribute("data-view-panel") !== target;
        });
        trackEvent("view_toggle", { view: target, group: group.getAttribute("data-toggle-group") });
      });
    });
  });
})();

// Guess-the-movie games (emoji clue / famous quote). Fires once per reveal
// — the <details> "toggle" event covers both click and keyboard activation.
// Also drives the "X / N revealed" progress counter and end-of-post
// completion message (see .game-progress/.game-complete in build_site.py
// and their CSS) when those elements exist on the page — they only get
// rendered for trivia-format posts, so this is a no-op everywhere else
// (quiz pages, plain listicles) since the querySelector calls just come
// back empty.
(function () {
  var reveals = document.querySelectorAll("details.reveal");
  if (!reveals.length) return;

  var progressEl = document.getElementById("game-progress");
  var progressCountEl = progressEl ? progressEl.querySelector(".game-progress-count") : null;
  var completeEl = document.getElementById("game-complete");
  var total = progressEl ? parseInt(progressEl.getAttribute("data-total") || "0", 10) : 0;

  // Tracks which items have already been counted, keyed by their
  // data-item value — re-opening a clue you already revealed shouldn't
  // increment the counter a second time.
  var revealedIds = {};
  var revealedCount = 0;

  reveals.forEach(function (details) {
    details.addEventListener("toggle", function () {
      if (!details.open) return;

      var item = details.getAttribute("data-item") || undefined;
      trackEvent("game_reveal", { item: item });

      if (progressCountEl && item && !revealedIds[item]) {
        revealedIds[item] = true;
        revealedCount++;
        progressCountEl.textContent = revealedCount;

        if (total && revealedCount >= total && completeEl) {
          completeEl.hidden = false;
          trackEvent("game_completed", { total: total });
        }
      }
    });
  });
})();

// Zoomed-poster guessing game (see render_poster_guess() in build_site.py
// and its CSS). Pick the right title out of a shrinking set of
// multiple-choice buttons while the poster zooms out one notch per wrong
// pick — always eventually solvable since choices only ever shrink.
// Originally this also chained into a guess-the-year slider per movie;
// that's now its own standalone format below (render_year_guess() /
// .year-guess-item), since bundling both into one round added a second
// interaction after the win, muddied one clean "solved in N tries" score
// into two different kinds of stats, and made for a wordier social pitch.
(function () {
  var items = document.querySelectorAll(".poster-guess-item");
  if (!items.length) return;

  var completeEl = document.getElementById("game-complete");
  var summaryEl = document.getElementById("poster-guess-summary");
  var total = items.length;
  var finishedCount = 0;
  var totalReveals = 0;

  items.forEach(function (item) {
    var img = item.querySelector(".poster-guess-img");
    var statusEl = item.querySelector(".poster-guess-status");
    var choiceButtons = Array.prototype.slice.call(item.querySelectorAll(".poster-guess-choice"));
    var realTitle = item.getAttribute("data-title");

    var reveals = 0;
    var startZoom = 3.2;
    var endZoom = 1;
    var stepsTotal = choiceButtons.length - 1;

    // Linear interpolation from startZoom down to endZoom over however
    // many wrong guesses this particular round actually has available
    // (choiceButtons.length varies — posts with fewer than 4 movies in
    // them produce fewer decoys, so this can't assume exactly 3 steps).
    function zoomFor(step) {
      if (stepsTotal <= 0) return endZoom;
      var frac = Math.min(step / stepsTotal, 1);
      return startZoom - frac * (startZoom - endZoom);
    }

    choiceButtons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (btn.disabled) return;

        if (btn.getAttribute("data-correct") === "true") {
          choiceButtons.forEach(function (b) { b.disabled = true; });
          btn.classList.add("poster-guess-correct");
          if (img) img.style.setProperty("--pg-zoom", "1");
          statusEl.textContent = reveals === 0
            ? "Got it on the first look — " + realTitle + "!"
            : "Solved in " + (reveals + 1) + " tries — " + realTitle;
          totalReveals += reveals;
          trackEvent("poster_guess_title", { reveals: reveals, title: realTitle });

          finishedCount++;
          if (finishedCount >= total && completeEl) {
            completeEl.hidden = false;
            if (summaryEl) {
              var avgReveals = (totalReveals / total).toFixed(1);
              summaryEl.textContent = "Averaged " + avgReveals + " reveals per movie.";
              refreshShareText("poster-guess-share", "I averaged " + avgReveals + " reveals per movie on The Clapperboard's zoomed poster game 🎬 Think you can beat it?");
            }
            trackEvent("poster_guess_completed", { total: total });
          }
        } else {
          btn.disabled = true;
          btn.classList.add("poster-guess-wrong");
          reveals++;
          if (img) img.style.setProperty("--pg-zoom", String(zoomFor(reveals)));
        }
      });
    });
  });
})();

// Guess-the-release-year game (see render_year_guess() in build_site.py
// and its CSS). Standalone sibling to the poster-guess game above — one
// slider per movie, scored by how far the guess lands from the real
// year, no reveal/identification step involved.
(function () {
  var items = document.querySelectorAll(".year-guess-item");
  if (!items.length) return;

  var completeEl = document.getElementById("game-complete");
  var summaryEl = document.getElementById("year-guess-summary");
  var total = items.length;
  var finishedCount = 0;
  var totalDiff = 0;

  items.forEach(function (item) {
    var slider = item.querySelector(".year-guess-slider");
    var output = item.querySelector(".year-guess-output");
    var lockBtn = item.querySelector(".year-guess-lock");
    var resultEl = item.querySelector(".year-guess-result");
    var realYear = parseInt(item.getAttribute("data-year"), 10);

    if (slider && output) {
      slider.addEventListener("input", function () {
        output.textContent = slider.value;
      });
    }

    if (lockBtn && slider) {
      lockBtn.addEventListener("click", function () {
        if (lockBtn.disabled) return;
        lockBtn.disabled = true;
        slider.disabled = true;

        var guess = parseInt(slider.value, 10);
        var diff = Math.abs(guess - realYear);
        resultEl.textContent = diff === 0
          ? "Nailed it exactly — " + realYear + "!"
          : "You guessed " + guess + " — it was actually " + realYear + " (off by " + diff + ").";
        resultEl.hidden = false;
        totalDiff += diff;
        trackEvent("year_guess_answer", { diff: diff, year: realYear });

        finishedCount++;
        if (finishedCount >= total && completeEl) {
          completeEl.hidden = false;
          if (summaryEl) {
            var avgDiff = (totalDiff / total).toFixed(1);
            summaryEl.textContent = "Averaged " + avgDiff + " years off per guess.";
            refreshShareText("year-guess-share", "I averaged " + avgDiff + " years off guessing movie release years on The Clapperboard 🗓️ Can you do better?");
          }
          trackEvent("year_guess_completed", { total: total });
        }
      });
    }
  });
})();

// "Which character are you" personality quizzes. Entirely client-side and
// generic across every quiz post — it just reads whatever .quiz-question /
// .quiz-result markup build_site.py generated for that page. Nothing here
// is sent anywhere; the tally only ever lives in the visitor's own browser.
//
// Only one question is ever shown at a time (the rest sit at display:none
// via the .active class) — picking an answer auto-advances to the next
// question after a short pause, and the last answer triggers scoring
// directly. This is what keeps the page from dumping all of a quiz's
// questions and answer text on the visitor at once.
(function () {
  var quizzes = document.querySelectorAll(".quiz");
  if (!quizzes.length) return;

  // Answer order (and therefore which letter badge lands on which result)
  // is shuffled fresh on every page load. build_site.py always writes the
  // same fixed order, so without this, a given letter would map to the
  // same character on every single question — reshuffling here is what
  // keeps the quiz from being reverse-engineerable at a glance.
  function shuffleAnswers(fieldset) {
    var container = fieldset.querySelector(".quiz-answers");
    var answers = Array.prototype.slice.call(container.querySelectorAll(".quiz-answer"));
    for (var i = answers.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = answers[i];
      answers[i] = answers[j];
      answers[j] = tmp;
    }
    answers.forEach(function (answer, i) {
      container.appendChild(answer); // re-insert in shuffled order
      var badge = answer.querySelector(".quiz-answer-badge");
      if (badge) badge.textContent = String.fromCharCode(65 + i); // A, B, C...
    });
  }

  quizzes.forEach(function (quiz) {
    var questionsWrap = quiz.querySelector(".quiz-questions");
    var resultsWrap = quiz.querySelector(".quiz-results");
    var fieldsets = Array.prototype.slice.call(quiz.querySelectorAll(".quiz-question"));
    var results = quiz.querySelectorAll(".quiz-result");
    var progressFill = quiz.querySelector(".quiz-progress-fill");
    var progressCurrent = quiz.querySelector(".quiz-progress-current");
    var total = fieldsets.length;
    var quizSlug = quiz.getAttribute("data-quiz");

    fieldsets.forEach(shuffleAnswers);

    function goToQuestion(index) {
      fieldsets.forEach(function (fs, i) {
        fs.classList.toggle("active", i === index);
      });
      if (progressFill) progressFill.style.width = (((index + 1) / total) * 100) + "%";
      if (progressCurrent) progressCurrent.textContent = index + 1;
    }

    function tallyAndShowResult() {
      var tally = {};
      fieldsets.forEach(function (fs) {
        var checked = fs.querySelector("input:checked");
        if (checked) tally[checked.value] = (tally[checked.value] || 0) + 1;
      });

      var winner = null;
      var winnerCount = -1;
      Object.keys(tally).forEach(function (key) {
        if (tally[key] > winnerCount) {
          winner = key;
          winnerCount = tally[key];
        }
      });
      if (!winner) return;

      results.forEach(function (r) {
        r.hidden = r.getAttribute("data-result") !== winner;
      });
      questionsWrap.hidden = true;
      resultsWrap.hidden = false;
      quiz.scrollIntoView({ behavior: "smooth", block: "start" });
      trackEvent("quiz_completed", { quiz: quizSlug, result: winner });
    }

    fieldsets.forEach(function (fs, index) {
      fs.querySelectorAll('input[type="radio"]').forEach(function (input) {
        input.addEventListener("change", function () {
          if (index === 0) trackEvent("quiz_start", { quiz: quizSlug });
          trackEvent("quiz_question_answered", { quiz: quizSlug, question: index + 1 });

          window.setTimeout(function () {
            if (index < total - 1) {
              goToQuestion(index + 1);
            } else {
              tallyAndShowResult();
            }
          }, 320);
        });
      });
    });

    quiz.querySelectorAll(".quiz-retake-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        trackEvent("quiz_retake", { quiz: quizSlug });
        fieldsets.forEach(function (fs) {
          fs.querySelectorAll("input:checked").forEach(function (input) {
            input.checked = false;
          });
        });
        resultsWrap.hidden = true;
        questionsWrap.hidden = false;
        goToQuestion(0);
        quiz.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  });
})();
