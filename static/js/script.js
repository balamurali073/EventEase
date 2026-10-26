/**
 * EventEase - Core Client-Side Logic
 * Handles countdown timer, ticket price calculations, organizer Chart.js, and dynamic ticket tiers.
 */

document.addEventListener('DOMContentLoaded', () => {
  initCountdownTimer();
  initTicketCalculator();
  initDynamicTicketTierAdder();
  initOrganizerRevenueChart();
});

// -------------------------------------------------------------
// 1. Checkout 10-Minute Hold Countdown Timer
// -------------------------------------------------------------
function initCountdownTimer() {
  const timerElement = document.getElementById('reservationTimer');
  if (!timerElement) return;

  let remainingSeconds = parseInt(timerElement.dataset.seconds, 10);
  const token = timerElement.dataset.token;
  const payBtn = document.getElementById('btnSubmitPayment');

  function updateClockDisplay() {
    if (remainingSeconds <= 0) {
      timerElement.textContent = "00:00 - EXPIRED";
      timerElement.className = "timer-clock pulse-danger";
      if (payBtn) {
        payBtn.disabled = true;
        payBtn.textContent = "Hold Expired (Restocked)";
      }
      alert("Your 10-minute hold has expired. The tickets have been released back into inventory.");
      window.location.reload();
      return;
    }

    const minutes = Math.floor(remainingSeconds / 60);
    const seconds = remainingSeconds % 60;
    timerElement.textContent = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;

    if (remainingSeconds <= 60) {
      timerElement.className = "timer-clock pulse-danger";
    } else if (remainingSeconds <= 180) {
      timerElement.className = "timer-clock pulse-warning";
    } else {
      timerElement.className = "timer-clock";
    }

    remainingSeconds--;
  }

  // Initial render and 1-second interval
  updateClockDisplay();
  const intervalId = setInterval(updateClockDisplay, 1000);

  // Sync with server every 20 seconds to prevent client-side clock drift
  if (token) {
    setInterval(() => {
      fetch(`/api/reservation-status/${token}`)
        .then(res => res.json())
        .then(data => {
          if (!data.valid) {
            clearInterval(intervalId);
            remainingSeconds = 0;
            updateClockDisplay();
          } else {
            remainingSeconds = data.remaining_seconds;
          }
        })
        .catch(err => console.error("Timer sync error:", err));
    }, 20000);
  }
}

// -------------------------------------------------------------
// 2. Event Detail Ticket Price Calculator
// -------------------------------------------------------------
function initTicketCalculator() {
  const ticketRadios = document.querySelectorAll('input[name="ticket_type_id"]');
  const quantityInput = document.getElementById('ticketQuantity');
  const totalDisplay = document.getElementById('calcTotalAmount');
  const subtotalDisplay = document.getElementById('calcUnitPrice');

  if (!quantityInput || !totalDisplay) return;

  function calculate() {
    let selectedPrice = 0;
    const checkedRadio = document.querySelector('input[name="ticket_type_id"]:checked');
    if (checkedRadio) {
      selectedPrice = parseFloat(checkedRadio.dataset.price || 0);
    }

    const qty = parseInt(quantityInput.value, 10) || 1;
    const total = selectedPrice * qty;

    if (subtotalDisplay) subtotalDisplay.textContent = `$${selectedPrice.toFixed(2)}`;
    totalDisplay.textContent = `$${total.toFixed(2)}`;
  }

  ticketRadios.forEach(radio => radio.addEventListener('change', calculate));
  quantityInput.addEventListener('input', calculate);
  calculate();
}

// -------------------------------------------------------------
// 3. Dynamic Multi-Ticket Tier Adder for Organizers
// -------------------------------------------------------------
function initDynamicTicketTierAdder() {
  const addBtn = document.getElementById('btnAddTicketTier');
  const container = document.getElementById('ticketTiersContainer');
  if (!addBtn || !container) return;

  addBtn.addEventListener('click', () => {
    const tierRow = document.createElement('div');
    tierRow.className = 'ticket-tier-input-row';
    tierRow.style.cssText = 'display: grid; grid-template-columns: 2fr 1fr 1fr auto; gap: 12px; margin-bottom: 12px; align-items: flex-end;';

    tierRow.innerHTML = `
      <div class="form-group" style="margin-bottom: 0;">
        <label class="form-label">Tier Name</label>
        <input type="text" name="ticket_name[]" class="form-control" placeholder="e.g. VIP Pass, Backstage" required>
      </div>
      <div class="form-group" style="margin-bottom: 0;">
        <label class="form-label">Price ($)</label>
        <input type="number" step="0.01" min="0" name="ticket_price[]" class="form-control" placeholder="99.00" required>
      </div>
      <div class="form-group" style="margin-bottom: 0;">
        <label class="form-label">Quantity</label>
        <input type="number" min="1" name="ticket_quantity[]" class="form-control" placeholder="100" required>
      </div>
      <button type="button" class="btn btn-danger btn-sm" style="height: 44px; padding: 0 14px;" onclick="this.closest('.ticket-tier-input-row').remove()">
        Remove
      </button>
    `;

    container.appendChild(tierRow);
  });
}

// -------------------------------------------------------------
// 4. Organizer Dashboard Chart.js Integration
// -------------------------------------------------------------
function initOrganizerRevenueChart() {
  const chartCanvas = document.getElementById('organizerRevenueChart');
  if (!chartCanvas || typeof Chart === 'undefined') return;

  fetch('/api/organizer/chart-data')
    .then(res => res.json())
    .then(data => {
      if (!data.labels || data.labels.length === 0) {
        chartCanvas.parentElement.innerHTML = '<p class="text-muted" style="text-align: center; padding: 40px;">No sales data available yet. Share your events to start generating revenue!</p>';
        return;
      }

      const ctx = chartCanvas.getContext('2d');
      
      // Purple gradient
      const gradient = ctx.createLinearGradient(0, 0, 0, 300);
      gradient.addColorStop(0, 'rgba(124, 58, 237, 0.8)');
      gradient.addColorStop(1, 'rgba(124, 58, 237, 0.1)');

      new Chart(ctx, {
        type: 'bar',
        data: {
          labels: data.labels,
          datasets: [{
            label: 'Revenue ($)',
            data: data.revenues,
            backgroundColor: gradient,
            borderColor: '#7C3AED',
            borderWidth: 2,
            borderRadius: 6
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              labels: {
                color: '#94A3B8',
                font: { family: 'Inter', size: 12 }
              }
            },
            tooltip: {
              backgroundColor: '#1E293B',
              titleColor: '#FFFFFF',
              bodyColor: '#A78BFA',
              borderColor: '#243042',
              borderWidth: 1,
              padding: 12,
              displayColors: false,
              callbacks: {
                label: function(context) {
                  return `Revenue: $${context.raw.toLocaleString()}`;
                }
              }
            }
          },
          scales: {
            x: {
              grid: { color: 'rgba(255, 255, 255, 0.05)' },
              ticks: { color: '#94A3B8', font: { family: 'Inter', size: 12 } }
            },
            y: {
              grid: { color: 'rgba(255, 255, 255, 0.05)' },
              ticks: {
                color: '#94A3B8',
                font: { family: 'Inter', size: 12 },
                callback: function(val) { return '$' + val; }
              }
            }
          }
        }
      });
    })
    .catch(err => console.error("Error loading chart data:", err));
}
