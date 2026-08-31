(function () {
  'use strict';

  const API = '/api/analytics';

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) {
      el.textContent = value;
    }
  }

  async function refreshPublicStats() {
    try {
      const response = await fetch(
        `${API}/public-stats`,
        {
          method: 'GET',
          credentials: 'include',
          cache: 'no-store'
        }
      );

      if (!response.ok) {
        console.error(
          'Public stats request failed:',
          response.status
        );
        return;
      }

      const data = await response.json();

      setText(
        'public-users-count',
        Number(data.users || 0).toLocaleString()
      );

      setText(
        'public-mocks-count',
        Number(data.mocks || 0).toLocaleString()
      );

      setText(
        'public-tests-count',
        Number(data.completed_tests || 0).toLocaleString()
      );

      setText(
        'public-active-count',
        Number(data.active_now || 0).toLocaleString()
      );

    } catch (error) {
      console.error(
        'Unable to load public stats:',
        error
      );
    }
  }

  async function heartbeat() {
    try {
      const response = await fetch(
        `${API}/heartbeat`,
        {
          method: 'POST',
          credentials: 'include',
          headers: {
            'Content-Type': 'application/json'
          },
          body: '{}'
        }
      );

      if (!response.ok && response.status !== 401) {
        console.error(
          'Heartbeat failed:',
          response.status
        );
      }

    } catch (error) {
      console.error(
        'Heartbeat error:',
        error
      );
    }
  }

  // Public counter
  refreshPublicStats();

  // Refresh public counters every 30 seconds.
  setInterval(
    refreshPublicStats,
    30000
  );

  // Logged-in activity.
  heartbeat();

  setInterval(
    heartbeat,
    60000
  );

  window.MockifyPublicStats = {
    refresh: refreshPublicStats,
    heartbeat: heartbeat
  };
})();