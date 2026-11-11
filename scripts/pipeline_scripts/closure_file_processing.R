install.packages("ggplot2")
install.packages("stringr")
install.packages("readr")
install.packages("tidyr")
install.packages("dplyr")
library(stringr)
library(ggplot2)
library(readr)
library(tidyr)
library(dplyr)

setwd(txt files)
closure_import=read.delim("schfile_extract_rectype_01_20250403.txt", header=FALSE)
names(closure_import)

closure_data=data.frame(closure_import)
names(closure_data)

closure_data$record_type=substr(closure_data$V1,1,2)
closure_data$opeid=substr(closure_data$V1,3,10)
closure_data$change_indicator=substr(closure_data$V1,11,11)
closure_data$school_name=substr(closure_data$V1,12,81)
closure_data$loc_name=substr(closure_data$V1,82,151)
closure_data$line1_adr=substr(closure_data$V1,152,186)
closure_data$line2_adr=substr(closure_data$V1,187,221)
closure_data$city=substr(closure_data$V1,222,246)
closure_data$state=substr(closure_data$V1,247,248)
closure_data$county=substr(closure_data$V1,249,251)
closure_data$country=substr(closure_data$V1,252,276)
closure_data$zip=substr(closure_data$V1,277,290)
closure_data$foreign_province=substr(closure_data$V1,291,315)
closure_data$elig_status_ind=substr(closure_data$V1,316,316)
closure_data$cert_type_cd=substr(closure_data$V1,317,317)
closure_data$approv_ind=substr(closure_data$V1,318,318)
closure_data$actn_cd=substr(closure_data$V1,319,320)
closure_data$actn_reason_cd=substr(closure_data$V1,321,322)
closure_data$actn_dt=substr(closure_data$V1,323,330)
closure_data$ppa_sent_dt=substr(closure_data$V1,331,338)
closure_data$ppa_execution_dt=substr(closure_data$V1,339,346)
closure_data$ppa_expiration_dt=substr(closure_data$V1,347,354)
closure_data$pgm_length=substr(closure_data$V1,355,356)
closure_data$sch_type=substr(closure_data$V1,357,357)
closure_data$acad_cal=substr(closure_data$V1,358,359)
closure_data$ethnic_cd=substr(closure_data$V1,360,360)
closure_data$filler1=substr(closure_data$V1,361,361)
closure_data$region_cd=substr(closure_data$V1,362,363)
closure_data$filler2=substr(closure_data$V1,364,371)
closure_data$sic_cd=substr(closure_data$V1,372,379)
closure_data$faads_cd=substr(closure_data$V1,380,381)
closure_data$close_dt=substr(closure_data$V1,382,389)
closure_data$init_appr_dt=substr(closure_data$V1,390,397)
closure_data$disapproval_dt=substr(closure_data$V1,398,405)
closure_data$location_reason=substr(closure_data$V1,406,407)
closure_data$filler3=substr(closure_data$V1,408,408)
closure_data$branch_ind=substr(closure_data$V1,409,409)
closure_data$case_team_cd=substr(closure_data$V1,410,411)
closure_data$reinstate_dt=substr(closure_data$V1,412,419)
closure_data$web_page=substr(closure_data$V1,420,469)
closure_data$filler4=substr(closure_data$V1,470,550)

closure_data=subset(closure_data,select=-c(V1,filler1,filler2,filler3,filler4))
names(closure_data)
View(closure_data)

closure_data$sch_type<-as.numeric(closure_data$sch_type)
closure_data<-filter(closure_data,closure_data$sch_type<4)

closure_data$close_year_str=substr(closure_data$close_dt,1,4)
closure_data$opeid6=substr(closure_data$opeid,1,6)
closure_data$branch_str=substr(closure_data$opeid,7,9)

closure_data$branch<-as.numeric(closure_data$branch_str)
closure_data$close_year<-as.numeric(closure_data$close_year_str)
closure_data$pgm_length<-as.numeric(closure_data$pgm_length)

closure_data$branch_count<-ave(closure_data$branch,closure_data$opeid6,FUN=max)
closure_data$branch_count<-closure_data$branch_count+1

closure_data$closed=case_when(closure_data$close_year!="NA"~1,closure_data$close_year=="NA"~0)
closure_data$closed_count<-ave(closure_data$closed,closure_data$opeid6,FUN=sum)
closure_data$close_flag=case_when(closure_data$branch_count==closure_data$closed_count~1)

closure_data$max_year_closed<-ave(closure_data$close_year,closure_data$opeid6,FUN=max)

closure_data<-filter(closure_data,closure_data$close_flag==1)

closure_data$latest_flag=case_when(closure_data$close_year==closure_data$max_year_closed~1)
closure_data$max_latest_flag<-ave(closure_data$latest_flag,closure_data$opeid6,FUN=max)

closure_data<-filter(closure_data,closure_data$close_year==closure_data$max_year_closed)

closure_data=subset(closure_data,select=c(school_name,state,opeid6,sch_type,max_year_closed,close_year,closed_count,pgm_length))

closure_data=closure_data[!duplicated(closure_data),]

length(unique(closure_data$opeid6))
length(unique(closure_data$opeid6))==nrow(closure_data)
closure_data$dup<-duplicated(closure_data$opeid6,fromLast=FALSE)
closure_data$dup2<-duplicated(closure_data$opeid,fromLast=TRUE)
closure_data$dup3=(case_when(closure_data$dup==TRUE | closure_data$dup2==TRUE~TRUE,closure_data$dup==FALSE & closure_data$dup2==FALSE~FALSE))

closure_data<-mutate(closure_data,state=case_when(dup3==TRUE~"Multiple",dup3=="FALSE"~closure_data$state))

closure_data=subset(closure_data,select=-c(dup,dup2,dup3,max_year_closed,row.names))
closure_data=closure_data[!duplicated(closure_data),]

setwd(csv files)
write.csv(closure_data,"closure_file_20250403.csv",row.names=FALSE)






