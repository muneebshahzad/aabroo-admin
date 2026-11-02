document.addEventListener("DOMContentLoaded", function() {
    // Function to check if it's within the allowed time range (9am to 10pm)
    function isTimeInRange() {
        var now = new Date();
        var startHour = 9; // 9 AM
        var endHour = 22; // 10 PM

        var currentHour = now.getHours();
        return currentHour >= startHour && currentHour < endHour;
    }

    // Function to show/hide start day button based on time and conditions
    function updateStartButtonVisibility() {
        var startDayButton = document.getElementById('startDayButton');
        if (isTimeInRange() && !localStorage.getItem('dayStarted')) {
            startDayButton.style.display = 'block';
        } else {
            startDayButton.style.display = 'none';
        }
    }

    // Function to show/hide resume day button based on time and conditions
    function updateResumeButtonVisibility() {
        var resumeDayButton = document.getElementById('resumeDayButton');
        if (localStorage.getItem('dayStarted')) {
            resumeDayButton.style.display = 'block';
        } else {
            resumeDayButton.style.display = 'none';
        }
    }

    // Update button visibility initially
    updateStartButtonVisibility();
    updateResumeButtonVisibility();

    // Event listener for starting the day
    document.getElementById('confirmStartBtn').addEventListener('click', function() {
        localStorage.setItem('dayStarted', true);
        updateStartButtonVisibility();
    });

    // Event listener for resuming the day
    document.getElementById('confirmResumeBtn').addEventListener('click', function() {
        // Resume day functionality here
        updateResumeButtonVisibility();
    });

    // Update button visibility every minute
    setInterval(function() {
        updateStartButtonVisibility();
        updateResumeButtonVisibility();
    }, 60000); // Update every minute
});