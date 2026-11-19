(function () {
  "use strict";
  var KS = window.KS;
  KS.$$("[data-invite-accept]").forEach(function (b) {
    b.addEventListener("click", async function () {
      // workers: the server measures the distance from the saved location (GPS or typed) — location may stay off
      KS.action("invitation.respond", { invitation_id: b.dataset.inviteAccept, accept: true, accept_terms: true });
    });
  });
})();
