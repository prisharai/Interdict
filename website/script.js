/* Interdict landing — interactions
   Blur-in reveals, hero headline morph, sticky lifecycle scroll scene,
   blast-radius counter, header inversion over dark sections, copy
   buttons, and the waitlist form. No dependencies. */

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ── 1. Reveal on scroll (blur + rise, Opacity-style) ─────────── */

const revealObserver = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        entry.target.classList.add("in");
        revealObserver.unobserve(entry.target);
      }
    }
  },
  { threshold: 0.25 }
);
document.querySelectorAll(".reveal").forEach((el) => revealObserver.observe(el));

/* ── 2. Hero headline blur-morph ──────────────────────────────── */

const morphLines = [...document.querySelectorAll(".morph-line")];
if (morphLines.length > 1 && !reduceMotion) {
  let current = 0;
  setInterval(() => {
    morphLines[current].classList.remove("is-active");
    current = (current + 1) % morphLines.length;
    morphLines[current].classList.add("is-active");
  }, 4600);
}

/* ── 3. Collage parallax (gentle, rAF-throttled) ──────────────── */

const parallaxCards = [...document.querySelectorAll(".mini-card[data-speed]")];
let parallaxTicking = false;
function applyParallax() {
  parallaxTicking = false;
  const vh = window.innerHeight;
  for (const card of parallaxCards) {
    const rect = card.parentElement.getBoundingClientRect();
    const offset = (rect.top + rect.height / 2 - vh / 2) * Number(card.dataset.speed);
    card.style.transform = `translateY(${offset}px)`;
  }
}
if (parallaxCards.length && !reduceMotion) {
  window.addEventListener(
    "scroll",
    () => {
      if (!parallaxTicking) {
        parallaxTicking = true;
        requestAnimationFrame(applyParallax);
      }
    },
    { passive: true }
  );
}

/* ── 4. Sticky lifecycle scene ────────────────────────────────── */

const lifecycleTrack = document.querySelector(".lifecycle-track");
const lifecycleScene = document.getElementById("lifecycleScene");
const lifeSteps = [...document.querySelectorAll(".life-step")];
const lifeLineFill = document.querySelector(".life-line-fill");
const headA = document.querySelector(".life-head.head-a");
const headB = document.querySelector(".life-head.head-b");
const STEP_THRESHOLDS = [0.08, 0.28, 0.48, 0.66, 0.84];

let blastStarted = false;

function updateLifecycle() {
  if (!lifecycleTrack || window.innerWidth <= 640) return;
  const rect = lifecycleTrack.getBoundingClientRect();
  const total = rect.height - window.innerHeight;
  const progress = Math.min(1, Math.max(0, -rect.top / total));

  lifeLineFill.style.setProperty("--progress", progress);
  lifeLineFill.style.transform = `scaleY(${progress})`;

  lifeSteps.forEach((step, i) => {
    step.classList.toggle("active", progress >= STEP_THRESHOLDS[i]);
  });

  // swap headline halfway through, like Opacity's two-phase scenes
  const secondHalf = progress > 0.5;
  headA.classList.toggle("is-active", !secondHalf);
  headB.classList.toggle("is-active", secondHalf);

  // kick off the blast-radius counter when its step lights up
  if (!blastStarted && progress >= STEP_THRESHOLDS[1]) {
    blastStarted = true;
    animateBlastCount();
  }
}

if (lifecycleTrack) {
  window.addEventListener("scroll", () => requestAnimationFrame(updateLifecycle), {
    passive: true,
  });
  updateLifecycle();
}

/* ── 5. Blast-radius counter ──────────────────────────────────── */

function animateBlastCount() {
  const el = document.querySelector(".blast-count");
  if (!el) return;
  const target = Number(el.dataset.target);
  if (reduceMotion) {
    el.textContent = target.toLocaleString("en-US");
    return;
  }
  const duration = 1400;
  const start = performance.now();
  function tick(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = Math.round(target * eased).toLocaleString("en-US");
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// mobile fallback: the scene isn't sticky, so count when it scrolls into view
if (window.innerWidth <= 640) {
  const blastEl = document.querySelector(".blast-count");
  if (blastEl) {
    new IntersectionObserver(
      (entries, obs) => {
        if (entries[0].isIntersecting) {
          animateBlastCount();
          obs.disconnect();
        }
      },
      { threshold: 0.6 }
    ).observe(blastEl);
  }
}

/* ── 6. Header inverts over dark sections ─────────────────────── */

const header = document.getElementById("siteHeader");
const darkSections = [...document.querySelectorAll("[data-dark]")];

function updateHeaderTheme() {
  const probeY = header.offsetHeight / 2;
  const onDark = darkSections.some((section) => {
    const rect = section.getBoundingClientRect();
    return rect.top <= probeY && rect.bottom >= probeY;
  });
  header.classList.toggle("on-dark", onDark);
}
window.addEventListener("scroll", () => requestAnimationFrame(updateHeaderTheme), {
  passive: true,
});
updateHeaderTheme();

/* ── 7. Copy-to-clipboard install buttons ─────────────────────── */

document.querySelectorAll(".copy-install").forEach((button) => {
  const hint = button.querySelector(".copy-hint");
  const original = hint.textContent;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      hint.textContent = "copied";
    } catch {
      hint.textContent = "select + copy";
    }
    setTimeout(() => (hint.textContent = original), 1600);
  });
});

/* ── 8. Waitlist form ─────────────────────────────────────────── */

// Paste a form endpoint here (Formspree, Basin, your own API…).
// Example: "https://formspree.io/f/xxxxxxxx"
const WAITLIST_ENDPOINT = "";
const FALLBACK_EMAIL = "pr482@cornell.edu";

const waitlistForm = document.getElementById("waitlistForm");
const waitlistNote = document.getElementById("waitlistNote");

if (waitlistForm) {
  waitlistForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const email = document.getElementById("waitlistEmail").value.trim();
    if (!email || !email.includes("@")) {
      waitlistNote.textContent = "Please enter a valid email address.";
      return;
    }

    if (!WAITLIST_ENDPOINT) {
      // no backend configured yet — fall back to a pre-filled email
      window.location.href =
        `mailto:${FALLBACK_EMAIL}` +
        `?subject=${encodeURIComponent("Interdict Team Cloud waitlist")}` +
        `&body=${encodeURIComponent(`Please add ${email} to the waitlist.`)}`;
      waitlistNote.textContent = "Opening your email client to complete signup…";
      return;
    }

    try {
      waitlistNote.textContent = "Joining…";
      const res = await fetch(WAITLIST_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ email }),
      });
      if (!res.ok) throw new Error(`status ${res.status}`);
      waitlistForm.reset();
      waitlistNote.textContent = "You're on the list. We'll be in touch soon.";
    } catch {
      waitlistNote.textContent =
        "Something went wrong — email us at " + FALLBACK_EMAIL + " instead.";
    }
  });
}
