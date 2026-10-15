//! Named download speed profiles with time-based scheduling (overnight-window aware).

use chrono::{Datelike, Local, Timelike};
use serde::{Deserialize, Serialize};

use crate::db::Database;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SpeedProfile {
    pub name: String,
    pub limit: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScheduleEntry {
    pub profile: String,
    pub start: String,
    pub end: String,
    #[serde(default = "default_days")]
    pub days: Vec<u8>,
}

fn default_days() -> Vec<u8> {
    vec![0, 1, 2, 3, 4, 5, 6]
}

pub fn default_profiles() -> Vec<SpeedProfile> {
    vec![SpeedProfile {
        name: "Unlimited".to_string(),
        limit: None,
    }]
}

pub fn parse_hhmm(value: &str) -> Result<u32, String> {
    let trimmed = value.trim();
    let Some((hh_str, mm_str)) = trimmed.split_once(':') else {
        return Err(format!("Invalid time '{}': use HH:MM (24h)", value));
    };
    if hh_str.len() != 2 || mm_str.len() != 2 {
        return Err(format!("Invalid time '{}': use HH:MM (24h)", value));
    }
    let hh: u32 = hh_str
        .parse()
        .map_err(|_| format!("Invalid hour in '{}'", value))?;
    let mm: u32 = mm_str
        .parse()
        .map_err(|_| format!("Invalid minute in '{}'", value))?;
    if hh > 23 || mm > 59 {
        return Err(format!("Invalid time '{}': use HH:MM (24h)", value));
    }
    Ok(hh * 60 + mm)
}

pub fn validate_profiles(profiles: Vec<SpeedProfile>) -> Result<Vec<SpeedProfile>, String> {
    if profiles.is_empty() {
        return Err("profiles must be a non-empty list of {name, limit}".to_string());
    }
    let mut seen = std::collections::HashSet::new();
    let mut clean = Vec::with_capacity(profiles.len());
    for p in profiles {
        let name: String = p.name.trim().chars().take(64).collect();
        if name.is_empty() {
            return Err("Each profile needs a non-empty name".to_string());
        }
        let lower = name.to_lowercase();
        if !seen.insert(lower) {
            return Err(format!("Duplicate profile name: {}", name));
        }
        let limit = match p.limit {
            Some(0) | None => None,
            Some(v) => Some(v),
        };
        clean.push(SpeedProfile { name, limit });
    }
    Ok(clean)
}

pub fn validate_schedule(
    schedule: Vec<ScheduleEntry>,
    profile_names: &[String],
) -> Result<Vec<ScheduleEntry>, String> {
    let known: std::collections::HashSet<String> =
        profile_names.iter().map(|s| s.to_lowercase()).collect();
    let mut clean = Vec::with_capacity(schedule.len());

    for entry in schedule {
        let profile = entry.profile.trim().to_string();
        if !known.contains(&profile.to_lowercase()) {
            return Err(format!("Schedule references unknown profile '{}'", profile));
        }
        let _ = parse_hhmm(&entry.start)?;
        let _ = parse_hhmm(&entry.end)?;
        let days = if entry.days.is_empty() {
            return Err(format!(
                "Invalid days for schedule entry '{}': use 0=Mon..6=Sun",
                profile
            ));
        } else {
            let mut d = entry.days.clone();
            if d.iter().any(|&x| x > 6) {
                return Err(format!(
                    "Invalid days for schedule entry '{}': use 0=Mon..6=Sun",
                    profile
                ));
            }
            d.sort_unstable();
            d.dedup();
            d
        };
        clean.push(ScheduleEntry {
            profile,
            start: entry.start.trim().to_string(),
            end: entry.end.trim().to_string(),
            days,
        });
    }
    Ok(clean)
}

pub fn match_schedule(schedule: &[ScheduleEntry]) -> Option<String> {
    let now = Local::now();
    let minute = now.hour() * 60 + now.minute();
    let weekday = now.weekday().num_days_from_monday() as u8;
    match_schedule_at(schedule, weekday, minute)
}

pub fn match_schedule_at(
    schedule: &[ScheduleEntry],
    weekday: u8,
    minute_of_day: u32,
) -> Option<String> {
    for entry in schedule {
        let Ok(start) = parse_hhmm(&entry.start) else {
            continue;
        };
        let Ok(end) = parse_hhmm(&entry.end) else {
            continue;
        };
        if !entry.days.contains(&weekday) {
            continue;
        }
        let inside = if start <= end {
            minute_of_day >= start && minute_of_day < end
        } else {
            // Overnight window, e.g. 22:00-07:00
            minute_of_day >= start || minute_of_day < end
        };
        if inside {
            return Some(entry.profile.clone());
        }
    }
    None
}

pub fn load_speed_config(
    db: Option<&Database>,
) -> (Vec<SpeedProfile>, Vec<ScheduleEntry>, Option<String>) {
    let mut profiles = default_profiles();
    let mut schedule = Vec::new();
    let mut override_name = None;

    let Some(db) = db else {
        return (profiles, schedule, override_name);
    };

    if let Some(raw_p) = db.get_setting("speed_profiles") {
        if let Ok(parsed) = serde_json::from_str::<Vec<SpeedProfile>>(&raw_p) {
            if let Ok(valid) = validate_profiles(parsed) {
                profiles = valid;
            }
        }
    }
    let names: Vec<String> = profiles.iter().map(|p| p.name.clone()).collect();
    if let Some(raw_s) = db.get_setting("speed_schedule") {
        if let Ok(parsed) = serde_json::from_str::<Vec<ScheduleEntry>>(&raw_s) {
            if let Ok(valid) = validate_schedule(parsed, &names) {
                schedule = valid;
            }
        }
    }
    if let Some(raw_o) = db.get_setting("speed_override") {
        let trimmed = raw_o.trim().to_string();
        if !trimmed.is_empty() {
            override_name = Some(trimmed);
        }
    }

    (profiles, schedule, override_name)
}

pub fn profile_limit(profiles: &[SpeedProfile], name: &str) -> Option<u64> {
    profiles
        .iter()
        .find(|p| p.name.eq_ignore_ascii_case(name))
        .and_then(|p| p.limit)
}
