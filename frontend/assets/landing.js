// StoryLiver marketing landing page - witness/unwitnessed proof toggle.
// A self-contained widget, deliberately not routed through app.js: this page
// loads before anyone has touched the real app state.
(() => {
  const witnessedBtn = document.getElementById("lpWitnessedBtn");
  const unwitnessedBtn = document.getElementById("lpUnwitnessedBtn");
  const box = document.getElementById("lpProofBox");
  const label = document.getElementById("lpProofLabel");
  const text = document.getElementById("lpProofText");
  const knowers = document.getElementById("lpKnowers");
  if (!witnessedBtn || !unwitnessedBtn || !box || !label || !text || !knowers) return;

  const COPY = {
    witnessed: {
      label: "YOU WERE SEEN",
      text: "The priest says nothing while you do it, which is worse. By morning the guild knows there is a ledger missing and knows whose hands were on the desk.",
      knowers: [
        { name: "THE PRIEST", cls: "lp-chip-ember" },
        { name: "TWO ALTAR BOYS", cls: "lp-chip-ember" },
        { name: "THE GUILD, BY MORNING", cls: "lp-chip-guild" },
      ],
    },
    unwitnessed: {
      label: "NOBODY SAW IT",
      text: "The rectory is empty and stays empty. The ledger is simply gone, and for eleven days the only person in the world who knows where it went is you.",
      knowers: [{ name: "ONLY YOU", cls: "lp-chip-solo" }],
    },
  };

  function render(witnessed) {
    const copy = witnessed ? COPY.witnessed : COPY.unwitnessed;
    box.classList.toggle("is-witnessed", witnessed);
    box.classList.toggle("is-unwitnessed", !witnessed);
    label.textContent = copy.label;
    text.textContent = copy.text;
    knowers.innerHTML = "";
    for (const k of copy.knowers) {
      const span = document.createElement("span");
      span.className = "lp-chip " + k.cls;
      span.textContent = k.name;
      knowers.appendChild(span);
    }
    witnessedBtn.setAttribute("aria-pressed", String(witnessed));
    unwitnessedBtn.setAttribute("aria-pressed", String(!witnessed));
  }

  witnessedBtn.addEventListener("click", () => render(true));
  unwitnessedBtn.addEventListener("click", () => render(false));
  render(true);
})();
