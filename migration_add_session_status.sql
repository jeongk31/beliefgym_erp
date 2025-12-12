-- Migration: Add session status tracking fields to schedules table
-- Run this in Supabase SQL Editor if you already have the schedules table

-- Add new columns
ALTER TABLE schedules
ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT '수업 계획' CHECK (status IN ('수업 계획', '수업 완료', '수업 취소'));

ALTER TABLE schedules
ADD COLUMN IF NOT EXISTS work_type VARCHAR(10);

ALTER TABLE schedules
ADD COLUMN IF NOT EXISTS session_signature TEXT;

ALTER TABLE schedules
ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE;
