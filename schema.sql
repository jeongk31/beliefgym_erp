-- Run this SQL in Supabase SQL Editor to set up the database

-- Branches table
CREATE TABLE IF NOT EXISTS branches (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Users table (trainers, branch admins, main admin)
CREATE TABLE IF NOT EXISTS users (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    name VARCHAR(100) NOT NULL,
    role VARCHAR(20) NOT NULL CHECK (role IN ('main_admin', 'branch_admin', 'trainer')),
    branch_id UUID REFERENCES branches(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Members table (회원)
CREATE TABLE IF NOT EXISTS members (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    member_name VARCHAR(100) NOT NULL,           -- 회원명
    phone VARCHAR(20) NOT NULL,                  -- 전화번호
    payment_method VARCHAR(50) NOT NULL,         -- 결제수단
    sessions INTEGER NOT NULL,                   -- 세션
    unit_price INTEGER NOT NULL,                 -- 단가
    channel VARCHAR(20) NOT NULL CHECK (channel IN ('WI', 'OT', '재등록', '소개')),  -- 경로
    signature TEXT,                              -- 사인 (base64 encoded image)
    trainer_id UUID NOT NULL REFERENCES users(id), -- 트레이너
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_by UUID REFERENCES users(id)
);

-- Schedules table (스케줄/시간표)
CREATE TABLE IF NOT EXISTS schedules (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    trainer_id UUID NOT NULL REFERENCES users(id),
    member_id UUID NOT NULL REFERENCES members(id),
    schedule_date DATE NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT '수업 계획' CHECK (status IN ('수업 계획', '수업 완료', '수업 취소')),
    work_type VARCHAR(10),  -- 근무내 or 근무외
    session_signature TEXT,  -- Member signature when completing session
    notes TEXT,
    completed_at TIMESTAMP WITH TIME ZONE,  -- When session was marked complete
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(trainer_id, schedule_date, start_time)  -- Prevent double booking
);

-- Insert sample branches
INSERT INTO branches (name) VALUES ('선릉점'), ('강남점'), ('역삼점');

-- Insert a main admin user (password: admin123)
-- Note: In production, use proper password hashing
INSERT INTO users (email, password_hash, name, role, branch_id)
VALUES ('admin@beliefgym.com', 'admin123', '관리자', 'main_admin', NULL);

-- Enable Row Level Security
ALTER TABLE members ENABLE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE branches ENABLE ROW LEVEL SECURITY;

-- Policies for public access (for demo purposes - tighten in production)
CREATE POLICY "Allow all access to branches" ON branches FOR ALL USING (true);
CREATE POLICY "Allow all access to users" ON users FOR ALL USING (true);
CREATE POLICY "Allow all access to members" ON members FOR ALL USING (true);
CREATE POLICY "Allow all access to schedules" ON schedules FOR ALL USING (true);

ALTER TABLE schedules ENABLE ROW LEVEL SECURITY;
