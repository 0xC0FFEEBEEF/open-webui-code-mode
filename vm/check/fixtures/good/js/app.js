function render(items) {
  const ul = document.getElementById("rows");
  ul.innerHTML = items.map((i) => `<li id="made">${i}</li>`).join("");
  const el = document.createElement("div"); el.id = "optional-widget";
  el.value = 3; el.customThing = true;
  ul.addEventListener("click", (ev) => { console.log(ev.target.value, ev.target.dataset.id); });
}
