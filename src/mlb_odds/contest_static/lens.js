// Shared head-to-head team lens renderer (D-034) for the contest pages.
// fetchLens(url) -> html string, or a dim error note.
async function lensHtml(url) {
  let m;
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
    m = await resp.json();
  } catch (e) {
    return `<div class="msg dim">team stats unavailable: ${e.message}</div>`;
  }
  const row = (r) => `<tr>
    <td class="num ${r.better === "away" ? "good" : ""}">${r.away ?? "–"}</td>
    <td class="dim lens-mid">${r.label}</td>
    <td class="num ${r.better === "home" ? "good" : ""}">${r.home ?? "–"}</td>
  </tr>`;
  return `<div class="lens-box">
    <div class="lens-head">
      <span><b>${m.away_team}</b> <span class="dim">${m.away_record ?? ""} · ${m.away_standing ?? ""}</span></span>
      <span class="dim">@</span>
      <span><b>${m.home_team}</b> <span class="dim">${m.home_record ?? ""} · ${m.home_standing ?? ""}</span></span>
    </div>
    <table class="lens-grid"><tbody>${m.rows.map(row).join("")}</tbody></table>
    <div class="msg dim" style="text-align:center">season stats via ESPN — green side leads</div>
  </div>`;
}
