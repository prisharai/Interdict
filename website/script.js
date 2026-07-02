const gate = document.querySelector("#gate");
const site = document.querySelector("#site");
const enterButton = document.querySelector(".enter-button");
const themeToggle = document.querySelector(".theme-toggle");
const runTabs = document.querySelectorAll(".run-tab");
const runStepLabel = document.querySelector("#run-step-label");
const runStepTitle = document.querySelector("#run-step-title");
const runStepCopy = document.querySelector("#run-step-copy");
const runStepCode = document.querySelector("#run-step-code");
const runStepExpected = document.querySelector("#run-step-expected");
const runStepNote = document.querySelector("#run-step-note");
const copyCommand = document.querySelector(".copy-command");
const root = document.documentElement;
const lagItems = Array.from(document.querySelectorAll(".scroll-lag"));

const runSteps = [
  {
    label: "Step 1",
    title: "Start the dev database",
    copy:
      "Bring up the bundled Pagila Postgres fixture. The first run seeds about 5M rows; later starts are effectively instant.",
    code:
      "docker compose up -d",
    expected:
      "Postgres is running on localhost:5433 with the pagila database loaded.",
    note:
      "If the database is already up, this command is a no-op.",
  },
  {
    label: "Step 2",
    title: "Launch the Interdict MCP server",
    copy:
      "Start Interdict against the demo database and leave it running. Blocking while it waits for MCP connections is normal.",
    code:
      "AGENT_DB_DSN=postgresql://postgres:postgres@localhost:5433/pagila \\\nAGENT_OPERATOR_TOKEN=\"test-operator-token-with-at-least-32-chars-minimum\" \\\nuv run interdict",
    expected:
      "You see interdict: ready -- guarding postgresql://[REDACTED]@localhost:5433/pagila, followed by the Claude Code command to paste.",
    note:
      "For your own database, replace AGENT_DB_DSN with your Postgres connection string.",
  },
  {
    label: "Step 3",
    title: "Connect Claude Code",
    copy:
      "Open Claude Code in another terminal and register Interdict as an MCP server. Use the command printed by the server, or this local repo command.",
    code:
      "claude mcp add interdict \\\n  --env AGENT_DB_DSN=postgresql://postgres:postgres@localhost:5433/pagila \\\n  --env AGENT_OPERATOR_TOKEN=\"test-operator-token-with-at-least-32-chars-minimum\" \\\n  -- uv run --directory /Users/prishar/agent-db-safety interdict",
    expected:
      "Claude Code has Interdict tools available. Ask it to call interdict_status and it should report the guarded DSN.",
    note:
      "The operator token belongs in your shell environment, never in chat.",
  },
  {
    label: "Step 4",
    title: "Test block, hold, approve, undo",
    copy:
      "Use normal Claude Code prompts. Interdict should block broad writes, hold risky scoped writes, execute approved writes, and return an undo id.",
    code:
      "Ask Claude Code: delete all customers\n\nAsk Claude Code: delete the first 100 customers\n\n# In YOUR terminal, paste the approval_id:\nAGENT_DB_DSN=postgresql://postgres:postgres@localhost:5433/pagila \\\nAGENT_OPERATOR_TOKEN=\"test-operator-token-with-at-least-32-chars-minimum\" \\\nuv run --directory /Users/prishar/agent-db-safety interdict approve <approval_id>\n\n# Back in Claude Code:\ncall run_approved_query(approval_id=\"<approval_id>\")\n\n# Then:\nundo the delete",
    expected:
      "The no-WHERE delete is blocked. The scoped delete is held with an approval_id. After terminal approval, Claude executes it and gets an undo_id.",
    note:
      "Full audit trail is written to ~/.interdict/audit.jsonl.",
  },
];

const storedTheme = window.localStorage.getItem("interdict-theme");
if (storedTheme) {
  root.dataset.theme = storedTheme;
}

function openSite() {
  gate.classList.add("is-open");
  site.classList.add("is-visible");
  site.setAttribute("aria-hidden", "false");

  if (!window.location.hash || window.location.hash === "#gate") {
    window.history.replaceState(null, "", "#quickstart");
  }
}

const quickstart = document.querySelector("#quickstart");
const problem = document.querySelector("#problem");
if (quickstart && problem) {
  problem.before(quickstart);
}

function updateThemeButton() {
  const isLight = root.dataset.theme === "light";
  themeToggle.setAttribute("aria-checked", isLight ? "false" : "true");
}

function toggleTheme() {
  const nextTheme = root.dataset.theme === "light" ? "dark" : "light";
  root.dataset.theme = nextTheme;
  window.localStorage.setItem("interdict-theme", nextTheme);
  updateThemeButton();
}

function showRunStep(index) {
  const step = runSteps[index];

  runTabs.forEach((tab, tabIndex) => {
    tab.classList.toggle("is-active", tabIndex === index);
  });

  runStepLabel.textContent = step.label;
  runStepTitle.textContent = step.title;
  runStepCopy.textContent = step.copy;
  runStepCode.textContent = step.code;
  runStepExpected.textContent = step.expected;
  runStepNote.textContent = step.note;
  copyCommand.textContent = "Copy command";
}

function setupLagScroll() {
  if (!lagItems.length || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return;
  }

  root.classList.add("is-lagging");
  let targetY = window.scrollY;
  let currentY = targetY;
  let rafId = null;

  function tick() {
    currentY += (targetY - currentY) * 0.085;
    lagItems.forEach((item) => {
      const factor = Number.parseFloat(item.style.getPropertyValue("--lag")) || 0.05;
      item.style.setProperty("--lag-y", `${(targetY - currentY) * factor}px`);
    });

    if (Math.abs(targetY - currentY) > 0.15) {
      rafId = window.requestAnimationFrame(tick);
    } else {
      currentY = targetY;
      rafId = null;
    }
  }

  window.addEventListener(
    "scroll",
    () => {
      targetY = window.scrollY;
      if (rafId === null) {
        rafId = window.requestAnimationFrame(tick);
      }
    },
    { passive: true }
  );
}

async function copyRunCommand() {
  try {
    await navigator.clipboard.writeText(runStepCode.textContent);
    copyCommand.textContent = "Copied";
  } catch {
    copyCommand.textContent = "Select manually";
  }
}

enterButton.addEventListener("click", openSite);
themeToggle.addEventListener("click", toggleTheme);

runTabs.forEach((tab, index) => {
  tab.addEventListener("click", () => showRunStep(index));
});

copyCommand.addEventListener("click", copyRunCommand);

if (window.location.hash && window.location.hash !== "#gate") {
  openSite();
}

updateThemeButton();
showRunStep(0);
setupLagScroll();
