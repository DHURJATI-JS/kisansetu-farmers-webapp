"""Static reference data for India: states/UTs, GST state codes, cities (lat/lon), national holidays."""
from __future__ import annotations

from datetime import date

STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
    "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
    "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
    "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal", "Andaman and Nicobar Islands", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
]

GST_STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
    "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh",
    "23": "Madhya Pradesh", "24": "Gujarat", "25": "Dadra and Nagar Haveli and Daman and Diu",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "28": "Andhra Pradesh",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory", "99": "Centre Jurisdiction",
}

# (city, state, lat, lon) — approximate centres, used for city filters, job geocoding & offline India checks
CITIES: list[tuple[str, str, float, float]] = [
    ("Delhi", "Delhi", 28.61, 77.21), ("Mumbai", "Maharashtra", 19.08, 72.88), ("Kolkata", "West Bengal", 22.57, 88.36),
    ("Chennai", "Tamil Nadu", 13.08, 80.27), ("Bengaluru", "Karnataka", 12.97, 77.59), ("Hyderabad", "Telangana", 17.39, 78.49),
    ("Ahmedabad", "Gujarat", 23.02, 72.57), ("Pune", "Maharashtra", 18.52, 73.86), ("Jaipur", "Rajasthan", 26.91, 75.79),
    ("Lucknow", "Uttar Pradesh", 26.85, 80.95), ("Kanpur", "Uttar Pradesh", 26.45, 80.33), ("Nagpur", "Maharashtra", 21.15, 79.09),
    ("Indore", "Madhya Pradesh", 22.72, 75.86), ("Bhopal", "Madhya Pradesh", 23.26, 77.41), ("Patna", "Bihar", 25.59, 85.14),
    ("Vadodara", "Gujarat", 22.31, 73.18), ("Surat", "Gujarat", 21.17, 72.83), ("Rajkot", "Gujarat", 22.30, 70.80),
    ("Ludhiana", "Punjab", 30.90, 75.86), ("Amritsar", "Punjab", 31.63, 74.87), ("Jalandhar", "Punjab", 31.33, 75.58),
    ("Patiala", "Punjab", 30.34, 76.39), ("Bathinda", "Punjab", 30.21, 74.95), ("Chandigarh", "Chandigarh", 30.73, 76.78),
    ("Agra", "Uttar Pradesh", 27.18, 78.01), ("Varanasi", "Uttar Pradesh", 25.32, 82.97), ("Prayagraj", "Uttar Pradesh", 25.44, 81.85),
    ("Meerut", "Uttar Pradesh", 28.98, 77.71), ("Gorakhpur", "Uttar Pradesh", 26.76, 83.37), ("Bareilly", "Uttar Pradesh", 28.37, 79.43),
    ("Aligarh", "Uttar Pradesh", 27.88, 78.08), ("Moradabad", "Uttar Pradesh", 28.84, 78.77), ("Jhansi", "Uttar Pradesh", 25.45, 78.57),
    ("Dehradun", "Uttarakhand", 30.32, 78.03), ("Haridwar", "Uttarakhand", 29.95, 78.16), ("Shimla", "Himachal Pradesh", 31.10, 77.17),
    ("Mandi", "Himachal Pradesh", 31.71, 76.93), ("Srinagar", "Jammu and Kashmir", 34.08, 74.80), ("Jammu", "Jammu and Kashmir", 32.73, 74.86),
    ("Leh", "Ladakh", 34.15, 77.58), ("Gurugram", "Haryana", 28.46, 77.03), ("Karnal", "Haryana", 29.69, 76.99),
    ("Hisar", "Haryana", 29.15, 75.72), ("Rohtak", "Haryana", 28.90, 76.61), ("Panipat", "Haryana", 29.39, 76.97),
    ("Jodhpur", "Rajasthan", 26.24, 73.02), ("Udaipur", "Rajasthan", 24.59, 73.71), ("Kota", "Rajasthan", 25.21, 75.86),
    ("Bikaner", "Rajasthan", 28.02, 73.31), ("Ajmer", "Rajasthan", 26.45, 74.64), ("Sri Ganganagar", "Rajasthan", 29.90, 73.88),
    ("Gwalior", "Madhya Pradesh", 26.22, 78.18), ("Jabalpur", "Madhya Pradesh", 23.18, 79.95), ("Ujjain", "Madhya Pradesh", 23.18, 75.78),
    ("Sagar", "Madhya Pradesh", 23.84, 78.74), ("Rewa", "Madhya Pradesh", 24.53, 81.30), ("Raipur", "Chhattisgarh", 21.25, 81.63),
    ("Bilaspur", "Chhattisgarh", 22.08, 82.15), ("Durg", "Chhattisgarh", 21.19, 81.28), ("Ranchi", "Jharkhand", 23.34, 85.31),
    ("Jamshedpur", "Jharkhand", 22.80, 86.20), ("Dhanbad", "Jharkhand", 23.80, 86.43), ("Gaya", "Bihar", 24.80, 85.00),
    ("Muzaffarpur", "Bihar", 26.12, 85.39), ("Bhagalpur", "Bihar", 25.24, 86.98), ("Darbhanga", "Bihar", 26.15, 85.90),
    ("Purnia", "Bihar", 25.78, 87.47), ("Bhubaneswar", "Odisha", 20.30, 85.82), ("Cuttack", "Odisha", 20.46, 85.88),
    ("Sambalpur", "Odisha", 21.47, 83.97), ("Berhampur", "Odisha", 19.31, 84.79), ("Balasore", "Odisha", 21.49, 86.93),
    ("Guwahati", "Assam", 26.14, 91.74), ("Dibrugarh", "Assam", 27.47, 94.91), ("Jorhat", "Assam", 26.75, 94.20),
    ("Silchar", "Assam", 24.83, 92.78), ("Tezpur", "Assam", 26.63, 92.80), ("Shillong", "Meghalaya", 25.58, 91.89),
    ("Imphal", "Manipur", 24.82, 93.94), ("Aizawl", "Mizoram", 23.73, 92.72), ("Agartala", "Tripura", 23.83, 91.28),
    ("Kohima", "Nagaland", 25.67, 94.11), ("Dimapur", "Nagaland", 25.91, 93.73), ("Itanagar", "Arunachal Pradesh", 27.08, 93.61),
    ("Gangtok", "Sikkim", 27.33, 88.61), ("Siliguri", "West Bengal", 26.73, 88.40), ("Darjeeling", "West Bengal", 27.04, 88.26),
    ("Durgapur", "West Bengal", 23.52, 87.31), ("Asansol", "West Bengal", 23.68, 86.98), ("Bardhaman", "West Bengal", 23.23, 87.86),
    ("Malda", "West Bengal", 25.01, 88.14), ("Kharagpur", "West Bengal", 22.35, 87.23), ("Visakhapatnam", "Andhra Pradesh", 17.69, 83.22),
    ("Vijayawada", "Andhra Pradesh", 16.51, 80.65), ("Guntur", "Andhra Pradesh", 16.31, 80.44), ("Nellore", "Andhra Pradesh", 14.44, 79.99),
    ("Kurnool", "Andhra Pradesh", 15.83, 78.04), ("Tirupati", "Andhra Pradesh", 13.63, 79.42), ("Anantapur", "Andhra Pradesh", 14.68, 77.60),
    ("Kakinada", "Andhra Pradesh", 16.99, 82.25), ("Warangal", "Telangana", 17.97, 79.59), ("Karimnagar", "Telangana", 18.44, 79.13),
    ("Nizamabad", "Telangana", 18.67, 78.09), ("Khammam", "Telangana", 17.25, 80.15), ("Mysuru", "Karnataka", 12.30, 76.64),
    ("Mangaluru", "Karnataka", 12.91, 74.86), ("Hubballi", "Karnataka", 15.36, 75.12), ("Belagavi", "Karnataka", 15.85, 74.50),
    ("Davanagere", "Karnataka", 14.46, 75.92), ("Ballari", "Karnataka", 15.14, 76.92), ("Kalaburagi", "Karnataka", 17.33, 76.83),
    ("Shivamogga", "Karnataka", 13.93, 75.57), ("Tumakuru", "Karnataka", 13.34, 77.10), ("Thiruvananthapuram", "Kerala", 8.52, 76.94),
    ("Kochi", "Kerala", 9.93, 76.27), ("Kozhikode", "Kerala", 11.26, 75.78), ("Thrissur", "Kerala", 10.53, 76.21),
    ("Palakkad", "Kerala", 10.79, 76.65), ("Kollam", "Kerala", 8.89, 76.61), ("Kannur", "Kerala", 11.87, 75.37),
    ("Idukki", "Kerala", 9.85, 76.97), ("Wayanad", "Kerala", 11.61, 76.08), ("Coimbatore", "Tamil Nadu", 11.02, 76.96),
    ("Madurai", "Tamil Nadu", 9.93, 78.12), ("Tiruchirappalli", "Tamil Nadu", 10.79, 78.70), ("Salem", "Tamil Nadu", 11.66, 78.15),
    ("Tirunelveli", "Tamil Nadu", 8.71, 77.76), ("Erode", "Tamil Nadu", 11.34, 77.72), ("Thanjavur", "Tamil Nadu", 10.79, 79.14),
    ("Vellore", "Tamil Nadu", 12.92, 79.13), ("Thoothukudi", "Tamil Nadu", 8.76, 78.13), ("Dindigul", "Tamil Nadu", 10.36, 77.98),
    ("Puducherry", "Puducherry", 11.94, 79.81), ("Panaji", "Goa", 15.49, 73.83), ("Margao", "Goa", 15.27, 73.96),
    ("Nashik", "Maharashtra", 20.00, 73.79), ("Chhatrapati Sambhajinagar", "Maharashtra", 19.88, 75.34), ("Solapur", "Maharashtra", 17.66, 75.91),
    ("Kolhapur", "Maharashtra", 16.70, 74.24), ("Amravati", "Maharashtra", 20.93, 77.75), ("Akola", "Maharashtra", 20.70, 77.00),
    ("Jalgaon", "Maharashtra", 21.01, 75.56), ("Latur", "Maharashtra", 18.40, 76.56), ("Nanded", "Maharashtra", 19.15, 77.31),
    ("Ahilyanagar", "Maharashtra", 19.09, 74.74), ("Sangli", "Maharashtra", 16.85, 74.58), ("Satara", "Maharashtra", 17.68, 74.00),
    ("Ratnagiri", "Maharashtra", 16.99, 73.30), ("Bhavnagar", "Gujarat", 21.76, 72.15), ("Jamnagar", "Gujarat", 22.47, 70.06),
    ("Junagadh", "Gujarat", 21.52, 70.46), ("Anand", "Gujarat", 22.56, 72.95), ("Mehsana", "Gujarat", 23.59, 72.37),
    ("Bhuj", "Gujarat", 23.24, 69.67), ("Sri Vijaya Puram", "Andaman and Nicobar Islands", 11.62, 92.73),
    ("Kavaratti", "Lakshadweep", 10.57, 72.64), ("Daman", "Dadra and Nagar Haveli and Daman and Diu", 20.40, 72.83),
    ("Silvassa", "Dadra and Nagar Haveli and Daman and Diu", 20.27, 73.01),
]
CITY_ALIASES = {
    "bangalore": "Bengaluru", "bombay": "Mumbai", "calcutta": "Kolkata", "madras": "Chennai", "gurgaon": "Gurugram",
    "allahabad": "Prayagraj", "aurangabad": "Chhatrapati Sambhajinagar", "ahmednagar": "Ahilyanagar", "mysore": "Mysuru",
    "mangalore": "Mangaluru", "hubli": "Hubballi", "belgaum": "Belagavi", "gulbarga": "Kalaburagi", "shimoga": "Shivamogga",
    "trivandrum": "Thiruvananthapuram", "cochin": "Kochi", "ernakulam": "Kochi", "calicut": "Kozhikode", "trichy": "Tiruchirappalli",
    "tuticorin": "Thoothukudi", "pondicherry": "Puducherry", "vizag": "Visakhapatnam", "port blair": "Sri Vijaya Puram",
    "new delhi": "Delhi", "baroda": "Vadodara", "benares": "Varanasi", "banaras": "Varanasi", "panjim": "Panaji",
}

# Gazetted national holidays (state/festival holidays are configured by the superadmin)
NATIONAL_HOLIDAYS = [((1, 26), "Republic Day"), ((8, 15), "Independence Day"), ((10, 2), "Gandhi Jayanti")]


def national_holidays_for(year: int) -> list[tuple[date, str]]:
    return [(date(year, m, d), name) for (m, d), name in NATIONAL_HOLIDAYS]


# helplines shown in the footer & AI answers
HELPLINES = {
    "kisan_call_centre": "1800-180-1551",
    "emergency": "112",
    "women": "181",
    "child": "1098",
}
