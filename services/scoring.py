import datetime
from models import EventDetails, UserProfile

class PriorityScorer:
    """Heuristic scoring for task priority."""
    @staticmethod
    def calculate_priority(title: str) -> int:
        title = title.lower()
        high = ["urgent", "meeting", "exam", "deadline", "client", "important", "interview"]
        low = ["watch", "browse", "surf", "chill", "maybe", "gym", "swimming"]
        
        if any(w in title for w in high): return 3
        if any(w in title for w in low): return 1
        return 2

class ScoringEngine:
    """
    Task prioritization logic:
    Calculates the score based on Priority, Deadlines, and Energy Fit.
    """
    
    @staticmethod
    def calculate_momentum_score(event: EventDetails, profile: UserProfile = None) -> float:
        """
        Scoring logic:
        Score = (Priority * 10) + (Urgency_Bonus) + (Energy_Fit)
        """
        base_score = float(event.priority * 10)
        
        # 1. Deadlines (Urgency Bonus)
        if event.deadline:
            # Logic for exponential score increase as deadline approaches
            base_score += 15.0
            
        # 2. Energy Fit (Level 2 Feature)
        if profile and event.start:
            energy_fit = ScoringEngine.get_energy_fit_score(event, profile)
            base_score += energy_fit
            
        return base_score

    @staticmethod
    def get_energy_fit_score(event: EventDetails, profile: UserProfile) -> float:
        """
        Matches task Energy Cost with User Energy Levels at that time.
        """
        try:
            dt = datetime.datetime.fromisoformat(event.start.replace('Z', '+00:00'))
            hour = dt.hour
            
            # Determine time category
            if 5 <= hour < 12: category = "morning"
            elif 12 <= hour < 17: category = "afternoon"
            elif 17 <= hour < 22: category = "evening"
            else: category = "night"
            
            user_energy = profile.energy_profile.get(category, 0.5)
            task_demand = event.energy_cost / 5.0 # Normalize 1-5 to 0.2-1.0
            
            # Bonus if high energy task matches peak user energy
            # Penalty if high energy task is in a 'slump' category
            fit_delta = 1.0 - abs(user_energy - task_demand)
            return fit_delta * 10.0 # Up to 10 points bonus
        except:
            return 0.0

    @staticmethod
    def assess_workload_health(events: list, profile: UserProfile) -> dict:
        """
        Calculates burnout risk and failure probability.
        """
        total_duration = 0
        total_energy_load = 0
        
        for e in events:
            # Assume 1 hour if not specified
            duration = 60 
            energy = 3 # Normal
            
            total_duration += duration
            total_energy_load += energy
            
        # Hard limits from profile
        max_duration = 480 # 8 hours
        max_load = 24 # 8 tasks * 3 energy
        
        failure_prob = (total_duration / max_duration) * 0.5 + (total_energy_load / max_load) * 0.5
        
        return {
            "failure_probability": round(min(failure_prob, 1.0), 2),
            "total_load": total_energy_load,
            "status": "danger" if failure_prob > 0.8 else "warning" if failure_prob > 0.6 else "healthy"
        }
