"""Local dashboard gate. Cloud installations should additionally use trusted SSO.

The shared password grants operator access to one deployment, not tenant identity.
"""
import hmac
import os
import time
from pathlib import Path

import streamlit as st


def configured_password() -> str | None:
    """Read a runtime-mounted secret without including it in errors or state."""
    path = os.getenv("DASHBOARD_PASSWORD_FILE")
    if path:
        try:
            return Path(path).read_text().strip() or None
        except (OSError, UnicodeError):
            return None
    return os.getenv("DASHBOARD_PASSWORD") or None


def require_authentication() -> None:
    if os.getenv("ALLOW_INSECURE_LOCAL", "").lower() == "true":
        st.caption("Local development mode: dashboard authentication is disabled.")
        return
    password = configured_password()
    if password is None or len(password.encode()) < 32:
        st.error("Dashboard access is unavailable. Ask the deployment operator to configure authentication.")
        st.stop()
    # Store a proof tied to the current secret, rather than the secret itself.
    import hashlib
    expected_proof = hashlib.sha256(password.encode()).hexdigest()
    if hmac.compare_digest(str(st.session_state.get("operator_proof", "")).encode(), expected_proof.encode()):
        if st.sidebar.button("Sign out"):
            st.session_state.pop("operator_proof", None)
            st.rerun()
        return
    now = time.monotonic()
    failures = st.session_state.get("login_failures", {"count": 0, "last": 0.0})
    if failures["count"] >= 5 and now - failures["last"] < 60:
        st.error("Too many failed attempts. Try again later.")
        st.stop()
    if now - failures["last"] >= 60:
        failures = {"count": 0, "last": 0.0}
    with st.form("dashboard_login"):
        supplied = st.text_input("Operator password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if hmac.compare_digest(supplied.encode(), password.encode()):
            st.session_state["operator_proof"] = expected_proof
            st.session_state.pop("login_failures", None)
            st.rerun()
        st.session_state["login_failures"] = {"count": failures["count"] + 1, "last": now}
        st.error("Authentication failed.")
    st.stop()


def may_acknowledge_alerts() -> bool:
    """Explicit deployment-wide operator capability; not a user-controlled field."""
    return os.getenv("DASHBOARD_ALLOW_ALERT_ACK", "").lower() == "true"
