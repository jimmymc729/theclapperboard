// Fires a GA4 custom event if the Google tag loaded (it can fail silently
// under ad blockers or with analytics consent declined, which is fine —
// every call site here is best-effort engagement data, never anything the
// site depends on functioning). GA already auto-attaches page_location to
// every event, so callers don't need to pass which page this happened on.
function trackEvent(name, params) {
  if (typeof gtag === "function") gtag("event", name, params || {});
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
