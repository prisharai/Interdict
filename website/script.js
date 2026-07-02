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
    label: "Step 0",
    title: "Install Interdict",
    copy:
      "Install the MCP server your agent will call before touching Postgres. The PyPI package name is interdict-db; the command it installs is interdict.",
    code:
      "pip install interdict-db\n\n# confirm the command exists\ninterdict --help",
    expected:
      "The interdict command is available. This is the MCP layer for Claude, Codex, Cursor, or any MCP-capable agent.",
    note:
      "Use interdict for agent integrations. It runs as the MCP safety layer between your agent and Postgres.",
  },
  {
    label: "Step 1",
    title: "Start the bundled test database",
    copy:
      "Bring up the seeded Postgres fixture. It includes Pagila plus large generated tables so you can safely watch blast-radius checks on realistic row counts.",
    code:
      "docker compose up -d\n\n# default DSN:\npostgresql://postgres:postgres@localhost:5433/pagila",
    expected:
      "Postgres is healthy on localhost:5433 with Pagila, metric_sample, and other test tables loaded.",
    note:
      "First start seeds millions of rows and can take a minute. Reset any experiment with docker compose down -v && docker compose up -d.",
  },
  {
    label: "Step 2",
    title: "Connect your agent",
    copy:
      "Register Interdict as an MCP server. The default policy works on any Postgres; use the Pagila policy only for the bundled demo database.",
    code:
      "codex mcp add interdict \\\n  --env AGENT_DB_DSN=postgresql://postgres:postgres@localhost:5433/pagila \\\n  --env AGENT_OPERATOR_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))') \\\n  -- interdict",
    expected:
      "The chat has Interdict tools available: run_query, revert_write, list_pending_approvals, run_approved_query, and interdict_status.",
    note:
      "For Claude Code, use the same shape with claude mcp add interdict ... -- interdict. Interdict now preflights policy loading and database reachability at startup.",
  },
  {
    label: "Step 3",
    title: "Verify Interdict is active",
    copy:
      "Interdict is active only in chats where the MCP server is connected. Verify the current chat before testing writes.",
    code:
      "# In Codex\n/mcp\n\n# Or ask the agent:\nCall interdict_status and tell me whether Interdict is active in this chat.",
    expected:
      "The agent reports active=true, the protected DSN, policy, audit health, simulation status, and undo status.",
    note:
      "Users should not need to say \"use Interdict\" every time. The agent should use it whenever a task needs database work.",
  },
  {
    label: "Step 4",
    title: "Run the practice prompts",
    copy:
      "Now use normal agent prompts on the bundled database. You should see one allowed read, one undoable write, one held write with an approval_id, and one blocked broad delete.",
    code:
      "Find actor_id 1 and summarize it.\n\nUpdate actor_id 1 by setting last_update = last_update, show the undo_action_id, then revert it.\n\nDelete rows from metric_sample where sensor_id <= 2000 and explain exactly why Interdict blocked or held it.",
    expected:
      "Allowed reads return rows. Reversible writes return undo_action_id. Held writes return approval_id and wait for interdict approve <id>; broad writes return block_reason.",
    note:
      "Held approvals expire after 30 minutes by default, so stale writes must be re-measured before a human can approve them.",
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
    window.history.replaceState(null, "", "#overview");
  }
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
